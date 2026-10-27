#!/usr/bin/env python3
"""Serve one file out of a torrent over HTTP, fetching only what is read.

lib/player/ltproc.py runs this with the system python, because libtorrent comes
from the python3-libtorrent package rather than from the application's
virtualenv. It is a torrent source for the pipeline in the same way peerflix is:
one line when it can serve, a status line every couple of seconds, and the file
itself over HTTP.

Why an HTTP layer of our own: a torrent file is mostly holes, and a reader that
reads into a hole gets zeroes rather than an error. Reads block until the pieces
they touch are on disk, and every blocked read says which pieces are wanted now,
so libtorrent spends the connection on those and on nothing else. A 1.9GB file
then costs the bytes that were read, not the bytes it contains: measured on a
Pi 3, a play 60 pieces in had fetched 120MB, and a seek cost the pieces it
landed in.

Two traps this code is shaped around, both measured:

- File priorities are applied on libtorrent's client thread. Piece priorities
  set before that lands are overwritten, the torrent decides it wants nothing,
  drops to "finished", sheds its peers and serves nothing -- which reads exactly
  like a swarm that has no peers for you. Everything here waits until the crop
  is observable before asking for a piece.
- A piece is complete in the disk cache before it is on disk, so reading the
  file directly can return the hole. Pieces are read with read_piece(), which
  goes through the same disk layer, and are handed out as the alert's buffer.
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: Pieces kept urgent ahead of the reader. Eight is about 15MB on a 1080p film
#: and measured enough to stay ahead of playback (delivered 1.3MB/s against a
#: 0.35MB/s film) while keeping the swarm interested: peers climbed to 88 with
#: only this window ever wanted.
WINDOW = 8

#: Pieces past the urgent window kept at low priority. They are the ones the
#: reader asks for next, and wanting them is what stops the torrent reporting
#: "finished" between reads. A finished torrent drops its swarm, and finding it
#: again costs seconds: measured, peers fell from 21 to 4 at the moment the
#: opening window completed, just as the player wanted more.
LOW_AHEAD = 4

#: How often the stage is told what is happening. Two seconds is what the old
#: stage polled peerflix at, and it is fast enough for a stall warning.
STATUS_EVERY = 2

#: Seconds to wait for metadata before giving up. Measured against a live swarm
#: it arrives in 1.5-4.3s; a minute and a half is generous, and failing here is
#: better than a bar that says "loading" for ever.
METADATA_TIMEOUT = 90

#: How long to wait for the file-priority crop to be observable.
CROP_TIMEOUT = 5.0

#: Resume data is written periodically as well as on the way out, because the
#: pipeline gives a stopping stage three seconds before SIGKILL.
RESUME_EVERY = 30

#: Resume data on the way out: inside the kill's grace period, with margin.
RESUME_GRACE = 2.0

#: Where the metadata and resume data for a torrent are kept. Inside the buffer
#: so that everything about a torrent is in one place, and hidden so that the
#: buffer still reads as a list of downloads.
RESUME_DIR = ".bf-torrent"

TAG = "bf-bt: "
STATUS_TAG = "bf-status: "


class Unsatisfiable(Exception):
    """A Range header that asks for bytes the file does not have."""


# --------------------------------------------------------------------------
# Pieces of logic worth testing without a swarm or a libtorrent install.


def parse_range(header, size):
    """
    The byte range a Range header asks for, inclusive.

    None means "the whole thing": no header, or one we do not understand, which
    HTTP says to answer with the whole entity. Unsatisfiable means the range is
    past the end, which has to be a 416: it is how the player learns the size.
    VLC asks for a range that starts at the length, and a server that answers
    200 to that streams a film it should have described in one line.
    """
    if not header:
        return None
    header = header.strip()
    if not header.lower().startswith("bytes="):
        return None
    spec = header.split("=", 1)[1].split(",")[0].strip()
    if "-" not in spec:
        return None
    first, last = (part.strip() for part in spec.split("-", 1))
    try:
        if not first:
            count = int(last)
            if count <= 0:
                raise Unsatisfiable(spec)
            return max(0, size - count), size - 1
        start = int(first)
        end = size - 1 if not last else min(int(last), size - 1)
    except ValueError:
        return None
    if start >= size or end < start:
        raise Unsatisfiable(spec)
    return start, end


def window_for(piece, last_piece, width=WINDOW):
    """The pieces to keep urgent while the reader is at piece."""
    lo = max(0, min(piece, last_piece))
    return lo, min(last_piece, lo + width - 1)


def pick_file(count, wanted):
    """
    Which file to play, and whether the request had to be overruled.

    An index out of range is a saved play whose file list changed under it, or a
    pack whose index was written by a different client. Playing the first file
    is what the old source did when it could not find the one asked for, and it
    is better than serving nothing.
    """
    if wanted is None or wanted < 0:
        return 0, False
    if wanted >= count:
        return 0, True
    return wanted, False


def top_entry(path):
    """
    The buffer entry a torrent-relative path lives in.

    The space check deletes whole entries and takes the one being played by
    name, so a pack must yield the folder and not the episode: deleting the
    folder would delete the film that is running.
    """
    parts = path.replace("\\", "/").split("/")
    return parts[0] if parts else ""


def resume_names(buffer_dir, info_hash):
    """Where the .torrent and its resume data live for one torrent."""
    base = os.path.join(buffer_dir, RESUME_DIR)
    return (
        os.path.join(base, info_hash + ".torrent"),
        os.path.join(base, info_hash + ".fastresume"),
    )


def hash_text(hashes):
    """An info_hash_t, v1 preferred, as hex for use in a file name."""
    return str(hashes.v1) if hashes.has_v1() else str(hashes.v2)


def file_priorities(count, playing, subs):
    """
    Every file unwanted, except the one being played and the subtitles.

    The played file has to be *wanted* even though its pieces are controlled
    one by one: libtorrent stores pieces of a priority-zero file in a hidden
    part file instead of the film's own, so a film watched that way is never a
    file on the card. Prioritised pieces of a wanted file are written in place,
    as a sparse film, and a later play -- or peerflix -- can see what is there.
    """
    priorities = [0] * count
    if 0 <= playing < count:
        priorities[playing] = 1
    for i in subs:
        if 0 <= i < count:
            priorities[i] = 1
    return priorities


def subtitle_pieces(count, ranges):
    """Ones for the given (first, last) piece ranges, zero elsewhere."""
    priorities = [0] * count
    for first, last in ranges:
        for piece in range(max(0, first), min(count - 1, last) + 1):
            priorities[piece] = 1
    return priorities


def error_text(err):
    """A libtorrent error_code as words, or "" for success."""
    if err is None:
        return ""
    try:
        if err.value() == 0:
            return ""
    except AttributeError:
        return str(err)
    try:
        message = err.message()
    except Exception:
        message = ""
    return message or "error %d" % err.value()


def atomic_write(path, data):
    """Write a file in one step, creating the directory that holds it."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Talking to libtorrent.


def load_libtorrent():
    try:
        import libtorrent
    except ImportError:
        print(
            TAG + "error libtorrent is not installed for " + sys.executable
            + "; run: sudo apt-get install python3-libtorrent",
            flush=True,
        )
        raise SystemExit(2)
    return libtorrent


def session_for(lt):
    """
    A session that finds peers and keeps its write cache small.

    The cache is RAM, and this box has learned what RAM used by a torrent costs:
    a verifier's page cache starved the display of contiguous memory and the
    picture failed to commit. 16MB held RSS at about 30MB in the measurements.
    """
    return lt.session(
        {
            "listen_interfaces": "0.0.0.0:0",
            "enable_dht": True,
            "enable_lsd": True,
            "enable_upnp": False,
            "enable_natpmp": False,
            "alert_mask": lt.alert.category_t.all_categories,
            "cache_size": 16 * 1024 * 1024,
        }
    )


class Alerts(threading.Thread):
    """
    The one reader of the session's alerts.

    Two callers need them: a read waiting for read_piece_alert, and the stop
    path waiting for save_resume_data_alert. Letting either pop alerts itself
    loses the other's, and libtorrent does not repeat them.
    """

    def __init__(self, session, lt):
        super().__init__(daemon=True)
        self.session = session
        self.lt = lt
        self.changed = threading.Condition()
        self.pieces = {}
        self.resume = None
        self.fatal = None

    def run(self):
        while True:
            self.session.wait_for_alert(200)
            for alert in self.session.pop_alerts():
                try:
                    self.handle(alert)
                except Exception as exc:  # a broken alert must not stop the pump
                    print(TAG + "alert handler failed: %r" % (exc,), flush=True)

    def handle(self, alert):
        name = type(alert).__name__
        if name == "read_piece_alert":
            error = error_text(alert.error)
            with self.changed:
                self.pieces[int(alert.piece)] = error or alert.buffer
                self.changed.notify_all()
        elif name == "save_resume_data_alert":
            with self.changed:
                self.resume = self.lt.write_resume_data_buf(alert.params)
                self.changed.notify_all()
        elif name in ("file_error_alert", "torrent_error_alert"):
            message = error_text(getattr(alert, "error", None)) or alert.message()
            with self.changed:
                self.fatal = message
                self.changed.notify_all()
            print(TAG + "error " + message, flush=True)

    def read_piece(self, handle, piece, timeout=120):
        """The piece's bytes, read through the disk layer, or raise."""
        with self.changed:
            self.pieces.pop(piece, None)
        handle.read_piece(piece)
        deadline = time.time() + timeout
        with self.changed:
            while piece not in self.pieces:
                if self.fatal:
                    raise IOError(self.fatal)
                left = deadline - time.time()
                if left <= 0:
                    raise IOError("piece %d could not be read" % piece)
                self.changed.wait(min(left, 0.5))
            data = self.pieces.pop(piece)
        if isinstance(data, str):
            raise IOError(data)
        return data


class Window:
    """The pieces the reader is at, kept urgent, and the ones behind it dropped."""

    def __init__(self, handle, last_piece, width=WINDOW):
        self.handle = handle
        self.last_piece = last_piece
        self.width = width
        self.lock = threading.Lock()
        self.asked = set()
        self.low = set()
        self.at = None

    def want(self, piece):
        """Ask for the window around piece and block until piece is here."""
        with self.lock:
            self._slide(piece)
            # the piece actually being read is due now, whatever the window says
            self.asked.add(piece)
            self.handle.piece_priority(piece, 7)
            self.handle.set_piece_deadline(piece, 0)
        while not self.handle.have_piece(piece):
            time.sleep(0.05)

    def ahead(self, piece):
        """Ask for the window around piece without waiting for it."""
        with self.lock:
            self._slide(piece)

    def span(self):
        with self.lock:
            if self.at is None:
                return None
            lo, hi = window_for(self.at, self.last_piece, self.width)
            return [lo, hi]

    def _slide(self, piece):
        self.at = piece
        lo, hi = window_for(piece, self.last_piece, self.width)
        for p in range(lo, hi + 1):
            if p in self.asked:
                continue
            self.asked.add(p)
            self.handle.piece_priority(p, 7)
            self.handle.set_piece_deadline(p, 300 + (p - lo) * 400)
        # A few pieces past the window at low priority: wanted, but never
        # urgent. See LOW_AHEAD.
        low_lo = hi + 1
        low_hi = min(hi + LOW_AHEAD, self.last_piece)
        for p in range(low_lo, low_hi + 1):
            if p in self.low:
                continue
            self.low.add(p)
            self.handle.piece_priority(p, 1)
        # A seek, or the player's probe at the end of the file, moves the
        # window; what it left behind is no longer worth a share of the line.
        # Pieces that did arrive are forgotten rather than released.
        for p in list(self.asked):
            if self.handle.have_piece(p):
                self.asked.discard(p)
            elif p < lo:
                self.asked.discard(p)
                self.handle.reset_piece_deadline(p)
                self.handle.piece_priority(p, 0)
        for p in list(self.low):
            if self.handle.have_piece(p):
                self.low.discard(p)
            elif p < low_lo or p > low_hi:
                self.low.discard(p)
                if p not in self.asked:
                    self.handle.piece_priority(p, 0)


class App:
    """The torrent, its files, and the reads being served from it."""

    def __init__(self, lt, session, handle, index, subs):
        self.lt = lt
        self.session = session
        self.handle = handle
        self.info = handle.torrent_file()
        self.piece_length = self.info.piece_length()
        self.last_piece = self.info.num_pieces() - 1
        self.files = self.info.files()
        self.index, self.clamped = pick_file(self.files.num_files(), index)
        self.subs = [i for i in subs if 0 <= i < self.files.num_files()]
        self.alerts = Alerts(session, lt)
        self.alerts.start()
        self.stopping = threading.Event()
        self.window = None
        self.started = time.time()
        self.metadata_after = None
        self.first_read_after = None

    def size(self, index):
        return self.files.file_size(index)

    def name(self, index):
        return os.path.basename(self.files.file_path(index))

    def top_entry(self, index):
        """The buffer entry this file lives in: the film, or the pack's folder."""
        return top_entry(self.files.file_path(index))

    def first_piece(self, index):
        """The first piece a file occupies."""
        offset = 0
        for i in range(index):
            offset += self.files.file_size(i)
        return offset // self.piece_length

    def last_piece_of(self, index):
        """The last piece a file occupies; before the first for an empty file."""
        offset = 0
        for i in range(index + 1):
            offset += self.files.file_size(i)
        if self.files.file_size(index) == 0:
            return self.first_piece(index) - 1
        return min((offset - 1) // self.piece_length, self.last_piece)

    def crop(self):
        """
        Want the played file only by piece, the subtitles whole, nothing else.

        The file is left wanted and every piece of it is switched off, then the
        window switches on the pieces the reader is at. Zeroing the file's
        priority instead would be fewer calls, and would put the film in
        libtorrent's part file rather than on the card: see file_priorities.
        """
        self.handle.prioritize_files(
            file_priorities(self.files.num_files(), self.index, self.subs)
        )
        # The crop lands on libtorrent's client thread. Asking for a piece
        # before it does gets the request overwritten by the crop, leaving a
        # torrent that wants nothing: see the note at the top of this file.
        first = self.first_piece(self.index)
        deadline = time.time() + CROP_TIMEOUT
        while time.time() < deadline:
            if self.handle.piece_priority(first) == 1:
                break
            time.sleep(0.05)
        # The played file is wanted, so its pieces are all on: switch every one
        # of them off again, leaving only the subtitles on, until the reader
        # says where it is. One call for all pieces rather than one per piece.
        started = time.time()
        self.handle.prioritize_pieces(
            subtitle_pieces(
                self.info.num_pieces(),
                [
                    (self.first_piece(i), self.last_piece_of(i))
                    for i in self.subs
                ],
            )
        )
        print(
            TAG + "pieces armed in %dms (%d-%d for file %d)"
            % ((time.time() - started) * 1000, first,
               self.last_piece_of(self.index), self.index),
            flush=True,
        )
        self.window = Window(self.handle, self.last_piece, WINDOW)

    def prime(self):
        """
        Ask for the opening pieces and the index at the end.

        The tail matters most: an MP4 whose index is at the end (no faststart)
        cannot start until the last piece is here, and a reader that has to ask
        for it blocks while nothing else is arriving. Asked for early and at
        moderate urgency it is usually there by the time the player wants it.
        """
        first = self.first_piece(self.index)
        for piece in (first, first + 1):
            if piece <= self.last_piece:
                self.window.ahead(piece)
        self.handle.piece_priority(self.last_piece, 7)
        self.handle.set_piece_deadline(self.last_piece, 3000)

    def wait(self, index, piece):
        if index == self.index:
            self.window.want(piece)
            if self.first_read_after is None:
                self.first_read_after = time.time() - self.started
        else:
            # a subtitle: the whole file is wanted, make the piece being read due
            self.handle.piece_priority(piece, 7)
            self.handle.set_piece_deadline(piece, 0)
            while not self.handle.have_piece(piece):
                if self.stopping.is_set():
                    raise IOError("stopping")
                time.sleep(0.05)

    def stream(self, index, start, end, write):
        """Send bytes start..end of a file, blocking until each piece is here."""
        size = self.size(index)
        stop = min(end, size - 1) + 1
        off = start
        while off < stop:
            piece = off // self.piece_length
            self.wait(index, piece)
            piece_end = min((piece + 1) * self.piece_length, size)
            chunk = min(stop, piece_end) - off
            data = self.alerts.read_piece(self.handle, piece)
            piece_start = piece * self.piece_length
            write(data[off - piece_start:off - piece_start + chunk])
            off += chunk
            if self.stopping.is_set():
                raise IOError("stopping")

    def status(self):
        st = self.handle.status()
        progress = self.handle.file_progress()
        files = []
        for i in range(self.files.num_files()):
            length = self.files.file_size(i)
            present = int(progress[i])
            files.append(
                {
                    "index": i,
                    "name": self.name(i),
                    "length": length,
                    "present": present,
                    "percent": int(present * 100 / length) if length else 0,
                }
            )
        return {
            "state": str(st.state),
            "peers": int(st.num_peers),
            "active_peers": int(st.num_connections),
            "seeds": int(st.num_seeds),
            "speed": int(st.download_rate),
            "downloaded": int(st.total_done),
            "uploaded": int(st.total_payload_upload),
            "pieces": [int(st.num_pieces), self.info.num_pieces()],
            "files": files,
            "playing": self.index,
            "clamped": self.clamped,
            "window": self.window.span() if self.window else None,
            "tail": bool(self.handle.have_piece(self.last_piece)),
            "metadata_after": (
                round(self.metadata_after, 1) if self.metadata_after else None
            ),
            "first_read_after": (
                round(self.first_read_after, 1)
                if self.first_read_after is not None else None
            ),
            "uptime": int(time.time() - self.started),
        }

    def close(self):
        self.stopping.set()


# --------------------------------------------------------------------------
# HTTP.


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "blissflixx-bt"

    @property
    def app(self):
        return self.server.app

    def log_message(self, fmt, *args):
        pass  # the stage logs what matters; one line per request is noise

    def do_HEAD(self):
        self.serve(write=None)

    def do_GET(self):
        self.serve(write=self.wfile.write)

    def serve(self, write):
        app = self.app
        path = self.path.split("?", 1)[0]
        if path == "/.json":
            return self.json_status()
        index = self.file_from_path(path)
        if index is None:
            return self.send_error(404, "no such file")
        size = app.size(index)
        try:
            span = parse_range(self.headers.get("Range"), size)
        except Unsatisfiable:
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        if span is None:
            start, end, code = 0, size - 1, 200
        else:
            start, end, code = span[0], span[1], 206
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if write is None:
            return
        try:
            app.stream(index, start, end, write)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the player seeked away, or probed the size and hung up
        except Exception as exc:
            print(TAG + "error serving %s: %s" % (path, exc), flush=True)

    def file_from_path(self, path):
        """'/' is the file being played; '/<n>' is file n, for subtitles."""
        app = self.app
        if path in ("", "/"):
            return app.index
        digits = path.strip("/")
        if not digits.isdigit():
            return None
        index = int(digits)
        if 0 <= index < app.files.num_files():
            return index
        return None

    def json_status(self):
        body = json.dumps(self.app.status()).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


# --------------------------------------------------------------------------
# Startup and the run loop.


def prepare(lt, source, buffer_dir):
    """
    The torrent's parameters and where its saved metadata lives.

    For a magnet the info hash is known before anything is added, which is what
    lets a restart find the .torrent and the resume data it wrote last time.
    """
    if source.startswith("magnet:"):
        params = lt.parse_magnet_uri(source)
        info_hash = hash_text(params.info_hashes)
    else:
        with urllib.request.urlopen(source, timeout=30) as response:
            data = response.read()
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(data)
        info_hash = hash_text(params.ti.info_hashes())
    torrent_path, resume_path = resume_names(buffer_dir, info_hash)
    if params.ti is not None and not os.path.isfile(torrent_path):
        try:
            atomic_write(
                torrent_path,
                lt.bencode(lt.create_torrent(params.ti).generate()),
            )
        except Exception as exc:
            print(TAG + "metadata not saved: %s" % exc, flush=True)
    return params, torrent_path, resume_path


def add_torrent(lt, session, params, buffer_dir, torrent_path, resume_path):
    """
    Add the torrent, reusing what is already on disk when we can.

    Resume data is what makes a second start instant: without it libtorrent
    hashes the whole buffer before it serves anything, which for a 2GB film is
    the 121 seconds the old source spent verifying. It also carries the
    bitfield, so a film that is entirely on the card starts with no swarm.
    """
    if os.path.isfile(torrent_path):
        try:
            params.ti = lt.torrent_info(torrent_path)
        except Exception as exc:
            print(TAG + "saved metadata ignored: %s" % exc, flush=True)
    if os.path.isfile(resume_path) and params.ti is not None:
        try:
            with open(resume_path, "rb") as f:
                resumed = lt.read_resume_data(f.read())
            resumed.ti = params.ti
            params = resumed
        except Exception as exc:
            print(TAG + "resume data ignored: %s" % exc, flush=True)
    params.save_path = buffer_dir
    return session.add_torrent(params)


def waiting_status(handle, started):
    st = handle.status()
    return {
        "state": "metadata",
        "peers": int(st.num_peers),
        "seeds": int(st.num_seeds),
        "speed": int(st.download_rate),
        "uptime": int(time.time() - started),
    }


def print_status(stats):
    print(STATUS_TAG + json.dumps(stats, separators=(",", ":")), flush=True)


def save_resume(app, path, timeout=RESUME_GRACE):
    """
    Ask for resume data and wait, briefly, for it to be produced.

    Never raises. This runs from the status loop, and a film must not stop
    because its bookkeeping could not be written: losing resume data costs a
    re-check on the next start, not the play that is running.
    """
    try:
        app.alerts.resume = None
        app.handle.save_resume_data()
        deadline = time.time() + timeout
        with app.alerts.changed:
            while app.alerts.resume is None and time.time() < deadline:
                app.alerts.changed.wait(0.1)
            data = app.alerts.resume
        if not data:
            return False
        atomic_write(path, data)
        return True
    except Exception as exc:
        print(TAG + "resume data not saved: %s" % exc, flush=True)
        return False


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", help="magnet or http(s) URL of a .torrent")
    parser.add_argument("--dir", required=True, help="where files are kept")
    parser.add_argument("--port", type=int, required=True, help="HTTP port")
    parser.add_argument("--index", type=int, default=0, help="file to play")
    parser.add_argument("--subs", default="", help="comma separated file indexes")
    parser.add_argument(
        "--metadata-timeout", type=float, default=METADATA_TIMEOUT
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    lt = load_libtorrent()
    os.makedirs(args.dir, exist_ok=True)
    subs = [int(part) for part in args.subs.split(",") if part.strip().isdigit()]

    session = session_for(lt)
    started = time.time()
    params, torrent_path, resume_path = prepare(lt, args.source, args.dir)
    handle = add_torrent(lt, session, params, args.dir, torrent_path, resume_path)

    deadline = started + args.metadata_timeout
    while not handle.has_metadata():
        session.wait_for_alert(500)
        print_status(waiting_status(handle, started))
        if time.time() > deadline:
            peers = handle.status().num_peers
            print(
                TAG + "error no metadata after %ds with %d peer(s); the torrent "
                "may have no sources" % (args.metadata_timeout, peers),
                flush=True,
            )
            return 3

    if not os.path.isfile(torrent_path):
        try:
            atomic_write(
                torrent_path,
                lt.bencode(lt.create_torrent(handle.torrent_file()).generate()),
            )
        except Exception as exc:
            print(TAG + "metadata not saved: %s" % exc, flush=True)

    app = App(lt, session, handle, args.index, subs)
    app.metadata_after = time.time() - started
    app.crop()
    server = Server(("127.0.0.1", args.port), app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app.prime()

    index = app.index
    print(
        TAG + "serving http://127.0.0.1:%d/ index=%d name=%s length=%d pieces=%d top=%s"
        % (args.port, index, app.name(index), app.size(index),
           app.info.num_pieces(), app.top_entry(index)),
        flush=True,
    )
    print_status(app.status())

    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    next_status = time.time()
    next_resume = time.time() + RESUME_EVERY
    while not stopping["now"]:
        time.sleep(0.2)
        if app.alerts.fatal:
            print(TAG + "error " + app.alerts.fatal, flush=True)
            return 4
        if time.time() >= next_status:
            next_status = time.time() + STATUS_EVERY
            print_status(app.status())
        if time.time() >= next_resume:
            next_resume = time.time() + RESUME_EVERY
            save_resume(app, resume_path)

    app.close()
    saved = save_resume(app, resume_path)
    print(
        TAG + "stopping, resume data %s" % ("saved" if saved else "not saved"),
        flush=True,
    )
    return 0


def run():
    """main(), but a traceback still ends with one line the stage can report."""
    try:
        return main()
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback

        traceback.print_exc()
        print(TAG + "error %s: %s" % (type(exc).__name__, exc), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(run())

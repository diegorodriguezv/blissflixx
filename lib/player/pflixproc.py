import json
import os
import re
import time

import cherrypy
import requests

from ..api.torrent import torrent2magnet
from ..locations import DATA_PATH
from .nodejs import node_binary
from .processpipe import TMP_DIR, ExternalProcess, ProcessException

_PEERFLIX_PORT = "9696"
#: Where peerflix keeps what it downloads. Told to peerflix with -f; it does not
#: create the directory itself.
#:
#: On the SD card, not in /tmp. This used to be /tmp/torrent-stream -- the
#: hardcoded default -- and moving it under /tmp/blissflixx was still wrong,
#: because on any current Pi /tmp is a tmpfs sized at half of RAM (systemd's
#: static tmp.mount, Options=...,size=50%%). A 1080p film is 2-4GB against
#: 371MB, so the download could not finish; it filled the mount and every
#: write after it failed with ENOSPC, which is how the on-screen confirmation
#: stopped appearing. peerflix's own .torrent library stays in /tmp on purpose:
#: that path is baked into peerflix and the file is ~21KB, so it is not worth
#: fighting for, and it is the only thing of ours left in RAM.
BUFFER_DIR = os.path.join(DATA_PATH, "downloads", "torrent-stream")

#: peerflix announces the address it is actually bound to, which is not always
#: loopback. It picks the first non-internal interface it finds, so on a Pi with
#: a wired connection it prints something like "server is listening on
#: http://192.168.1.119:9696/". Overriding that with 127.0.0.1 -- which is what
#: this used to do unconditionally -- hands the next stage an address peerflix
#: is not listening on, and the player fails with "cannot connect to
#: 127.0.0.1:9696" after sitting there for a minute.
_LISTENING_RE = re.compile(r"(http://\S+?)/?\s*$")

#: peerflix's own progress line while it hashes what is already on disk:
#: "Verifying downloaded: 27%". The colours around it are why this looks for the
#: number rather than the whole line.
_VERIFYING_RE = re.compile(r"Verifying downloaded:.*?(\d+)%")

#: How often the swarm's own numbers are read, and for how long.
#:
#: They are only wanted while the picture has not started -- a film that is
#: playing says what it is doing on the screen -- so the asking stops after a
#: few minutes rather than running for the length of the film.
_PROGRESS_EVERY = 2
_PROGRESS_FOR = 300

#: How long the numbers may stay identical before the bar says nothing is
#: arriving. Long enough that a slow piece does not read as a dead torrent,
#: short enough that a dead one is not left looking busy.
_STALLED_AFTER = 20

#: How often, in progress-watch ticks, the buffer's page cache is dropped while
#: the film starts. The watch ticks every _PROGRESS_EVERY seconds, so 30 is
#: about once a minute: often enough that the verifier's one-pass reads never
#: accumulate beside the film, seldom enough that the walk is noise.
_CACHE_DROP_EVERY = 30


def drop_buffer_cache():
    """
    Evict the torrent buffer's file pages from memory. Returns how many files
    were dropped.

    Verifying a buffered torrent reads gigabytes through the page cache, and
    on this box those pages land in the display memory's region faster than
    the kernel migrates them out: measured on rpi, a 60MB read cost 155MB of
    CmaFree, and a play starting with a pressured region decodes to a black
    screen -- "Atomic commit failed: No space left on device", which is the
    display, not the disk. The verifier reads each piece once and never again,
    so what has been read is never needed in memory; telling the kernel so
    keeps the region defragmented for the decoder's large contiguous
    allocations. Needs no privileges: an eviction hint on files this user can
    read.

    Best effort, so it never raises: a play must not fail because housekeeping
    did.
    """
    try:
        return _drop_buffer_cache()
    except Exception:
        return 0


def _drop_buffer_cache():
    if not os.path.isdir(BUFFER_DIR):
        return 0
    fadvise = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if fadvise is None or dontneed is None:
        return 0
    dropped = 0
    for root, _dirs, files in os.walk(BUFFER_DIR):
        for name in files:
            try:
                fd = os.open(os.path.join(root, name), os.O_RDONLY)
            except OSError:
                continue
            try:
                fadvise(fd, 0, 0, dontneed)
                dropped += 1
            except OSError:
                pass
            finally:
                os.close(fd)
    return dropped


class PeerflixProcess(ExternalProcess):
    def __init__(self, torrent, idx, subs=None):
        super().__init__()
        #: The last "Verifying downloaded: N%" seen, or None.
        self._verifying = None
        #: Whether this run reused a previous verification instead of hashing.
        #: A hit emits no verifying/verify lines, so without this the TIMING
        #: line would read "verified None%".
        self._cache_hit = False
        #: The swarm's last answer: downloaded / total / speed / peers.
        self._progress = None
        self._progress_at = None
        #: When the numbers last changed, for the stalled line.
        self._moved_at = None
        self._started_at = None
        #: The files inside this torrent to pull out as subtitles, most
        #: probable first: [{"idx": n, "name": ".."}]. Empty when this play
        #: asked for none.
        self.subs = subs or []
        cmd = [node_binary(), "--max-old-space-size=128", "/usr/local/bin/peerflix"]
        # Avoid problems with downloading torrent files
        torrent = torrent2magnet(torrent)
        cmd.append(torrent)
        cmd.append("-q")
        # -r used to be passed here. peerflix spells it --remove, "remove files
        # on exit", and it meant that every torrent started again from nothing:
        # the download could not survive being stopped, and someone with poor
        # peers or a poor connection lost the whole of their progress on every
        # attempt. Nothing deletes torrent data here now.
        # Verification stays on. torrent-stream hashes everything already on
        # disk before it opens the port, and that is what makes it know which
        # pieces it has: 121 seconds for a 2GB film already in the buffer,
        # against 4 seconds without. But without it the buffer cannot be used at
        # all -- measured, the port opens in four seconds and the film then waits
        # for peers to send what was already on disk. A watched film would stop
        # playing when its swarm went quiet.
        #
        # The real cost is the buffer growing without limit; see the note on
        # BUFFER_DIR and the cleanup in blissflixx.py.
        cmd.append("-p")
        cmd.append(_PEERFLIX_PORT)
        # Keep peerflix's buffer on the card rather than in /tmp, which on a
        # current Pi is a tmpfs of half of RAM and cannot hold a film. See
        # BUFFER_DIR.
        cmd.append("-f")
        cmd.append(BUFFER_DIR)
        if idx is not None and idx >= 0:
            cmd.append("-i")
            cmd.append(str(idx))
        self.cmd = cmd

    def name(self):
        return "peerflix"

    def _get_cmd(self, args):
        self.args = args
        return self.cmd

    def _ready(self):
        self._started_at = time.time()
        while True:
            line = self._readline()
            found = _VERIFYING_RE.search(line)
            if found:
                self._verifying = int(found.group(1))
            if line.startswith("bf-verify-cache: hit"):
                self._cache_hit = True
            if line.startswith("Bad Response"):
                raise ProcessException(line)
            # Get this error if site down/blocked
            # html page instead of torrent
            elif line.startswith("not a colon at"):
                raise ProcessException("Unable to retrieve torrent")
            elif line.startswith("server is listening"):
                self.args["outfile"] = self._advertised_url(line)
                if self._cache_hit:
                    cherrypy.log(
                        "TIMING peerflix %.1fs to serve (cache hit)"
                        % (time.time() - self._started_at)
                    )
                else:
                    cherrypy.log(
                        "TIMING peerflix %.1fs to serve (verified %s%%)"
                        % (time.time() - self._started_at, self._verifying)
                    )
                from .processpipe import _start_thread

                _start_thread(self._watch_progress)
                if self.subs:
                    # Where the subtitles live, not the subtitles: fetching one
                    # waits on pieces that may not be here yet, and waiting here
                    # delays the film. The player fetches them in the background
                    # and adds them mid-play. See VlcProcess._add_torrent_subtitles.
                    base = self.args["outfile"].rstrip("/")
                    self.args["subtitles_wanted"] = [
                        {
                            "url": base + "/" + str(s["idx"]),
                            "name": s.get("name") or "subtitle-" + str(s["idx"]),
                        }
                        for s in self.subs
                    ]
                return self.args

    def status_msg(self):
        """
        What the bar says before the port is open: the hashing, in numbers.

        peerflix hashes the whole buffer before it will serve anything, which
        for a film watched before is minutes with nothing on screen. The
        percentage is its own; it prints one per piece.
        """
        if self._verifying is not None:
            return "VERIFYING %d%%" % self._verifying
        return "LOADING STREAM"

    def detail(self):
        """
        The swarm's numbers, while the picture has not started.

        "12% · 1.4 MB/s · 6 peers", and when they stop moving, how long they
        have been still: a torrent that is not downloading has to look like one
        rather than like a film that is thinking about it.
        """
        if self._progress is None:
            return ""
        # The percentage is the swarm's bytes for *this* run, not the share of
        # the film on disk: a film already buffered reads 0% while needing
        # nothing. So it is shown only when this run has downloaded something,
        # and the speed and peers -- which are always true -- carry the line.
        parts = []
        if self._verifying:
            # The hashing is still running beside the film, now that the port
            # opens before it finishes. Nothing is said at 0%: the film is
            # playing, and "verifying 0%" beside "0%" reads as two failures.
            parts.append("verifying %d%%" % self._verifying)
        if self._progress["downloaded"]:
            parts.append("%d%%" % self._progress["percent"])
        parts.append("%s/s" % _size(self._progress["speed"]))
        parts.append("%d peers" % self._progress["peers"])
        text = " · ".join(parts)
        if self._moved_at is not None:
            quiet = int(time.time() - self._moved_at)
            if quiet >= _STALLED_AFTER:
                return text + " — nothing arriving for %ds" % quiet
        return text

    def _watch_progress(self):
        """
        Ask the swarm what it is doing, every couple of seconds, for a while.

        peerflix answers /.json with the piece of its own bookkeeping this
        needs: bytes downloaded against the torrent's length, the speed, and
        how many peers are connected. Cheap, and it stops on its own.
        """
        base = (self.args.get("outfile") or "").rstrip("/")
        if not base:
            return
        deadline = time.time() + _PROGRESS_FOR
        ticks = 0
        while time.time() < deadline:
            if self.killing:
                return
            ticks += 1
            if ticks % _CACHE_DROP_EVERY == 0:
                # Free-behind the verifier while it hashes: see
                # drop_buffer_cache. This loop already runs for exactly the
                # window the hashing runs in.
                drop_buffer_cache()
            try:
                r = requests.get(base + "/.json", timeout=5)
                stats = json.loads(r.content.decode("utf-8"))
            except Exception:
                time.sleep(_PROGRESS_EVERY)
                continue
            total = stats.get("totalLength") or 0
            downloaded = stats.get("downloaded") or 0
            if not total:
                time.sleep(_PROGRESS_EVERY)
                continue
            percent = int(downloaded * 100 / total)
            if self._progress is None or percent != self._progress["percent"]:
                self._moved_at = time.time()
            self._progress = {
                "downloaded": downloaded,
                "percent": percent,
                "speed": int(stats.get("downloadSpeed") or 0),
                "peers": int(stats.get("totalPeers") or 0),
            }
            self._progress_at = time.time()
            time.sleep(_PROGRESS_EVERY)

    def _advertised_url(self, line):
        """
        Take the URL peerflix printed rather than composing one.

        Falls back to loopback only when the line carries no usable URL, which
        keeps the old behaviour as a last resort instead of as the default.
        """
        match = _LISTENING_RE.search(line)
        if match:
            return match.group(1)
        cherrypy.log("peerflix listening line had no url: " + line)
        return "http://127.0.0.1:" + _PEERFLIX_PORT

    def stop(self):
        # Nothing is deleted here. This used to rmtree the download directory on
        # every stop, which discarded the whole download each time.
        super().stop()
        # Leave the box as it was found. Whatever the verifier read since the
        # last drop is still sitting in the display memory's region; the next
        # film's decoder needs that region defragmented, and nothing will read
        # these pages again until it is verified again.
        drop_buffer_cache()


def _size(value):
    """Bytes per second, in words."""
    if value >= 1024 * 1024:
        return "%.1f MB" % (value / (1024 * 1024))
    if value >= 1024:
        return "%d kB" % (value / 1024)
    return "%d B" % value

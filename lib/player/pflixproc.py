import json
import os
import re
import time

import cherrypy
import requests

from ..api.torrent import torrent2magnet
from ..locations import DATA_PATH
from . import buffer as buffer_space
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
#: the film starts. The watch ticks every _PROGRESS_EVERY seconds, so 5 is
#: about once every ten seconds. Once a minute was the first guess and it was
#: too slow: a local file failed at 0:30 from the same pressure, so the window
#: a play has to survive is tens of seconds, not a minute.
_CACHE_DROP_EVERY = 5


def drop_file_cache(path):
    """
    Evict one file's pages, or say zero. Never raises.

    The single-file form of drop_buffer_cache, for a film being read from the
    card: VLC reads its own media through the same page cache, and those pages
    land in the display memory's region just as the verifier's do. Measured on
    rpi with a 1080p file, the same play failed at 0:30 without this and ran
    past 1:35 with it, CmaFree steady at 64MB either way until the failure.
    """
    try:
        fadvise = getattr(os, "posix_fadvise", None)
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
        if fadvise is None or dontneed is None or not path:
            return 0
        fd = os.open(path, os.O_RDONLY)
        try:
            fadvise(fd, 0, 0, dontneed)
        finally:
            os.close(fd)
        return 1
    except OSError:
        return 0


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
        #: The last percentage seen, kept for the timing line after verification
        #: has ended and _verifying has been cleared.
        self._verified = None
        #: Whether this run reused a previous verification instead of hashing.
        #: A hit emits no verifying/verify lines, so without this the TIMING
        #: line would read "verified None%".
        self._cache_hit = False
        #: Whether the buffer has been evicted at the end of hashing. The
        #: copier feeds every line here for the whole run (see on_output_line),
        #: so the hashing just finished is visible while the decoder is about
        #: to set up -- the one moment a defragmented region matters most.
        #: Two milestones: the tail (cues live at the file's end, and the
        #: setup follows the first frame) and the finish.
        self._drop_marks = set()
        self.on_output_line = self._watch_verify
        #: The swarm's last answer: downloaded / total / speed / peers.
        self._progress = None
        self._progress_at = None
        #: When the numbers last changed, for the stalled line.
        self._moved_at = None
        #: The one space decision for this play, once the film's size is known:
        #: what free space it wanted, what it freed, and which entries went.
        #: In memory only; the panel reads it and the log records it.
        self._space_checked = False
        self._space_needed = 0
        self._space_freed = 0
        self._space_removed = []
        self._started_at = None
        #: The files inside this torrent to pull out as subtitles, most
        #: probable first: [{"idx": n, "name": ".."}]. Empty when this play
        #: asked for none.
        self.subs = subs or []
        #: Which file inside the torrent this play asked for, from -i.
        self.idx = idx
        #: When the swarm last had a peer at all. Zero peers for long enough
        #: while there is still film missing is the "nothing arriving" case the
        #: bar warns about; see stalled_for().
        self._peers_seen_at = None
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
                percent = int(found.group(1))
                # Same rule as the whole-run watcher: peerflix prints the
                # latest verified piece, and the swarm's downloads can be
                # behind the hashing front, so keep the highest seen.
                if self._verified is None or percent > self._verified:
                    self._verified = percent
                # 100% is the end of it. Holding the last number left the
                # panel saying "verify 0%" for the whole of a film whose
                # verification had stopped at its first piece, and "verify
                # 100%" on one that had finished -- neither is a thing the
                # player is doing.
                self._verifying = None if self._verified >= 100 else self._verified
            if line.startswith("bf-verify-cache: hit"):
                self._cache_hit = True
                # Nothing was hashed at all this run.
                self._verifying = None
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
                        % (time.time() - self._started_at, self._verified)
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
        # The share of the film that is *on the card*, said as such.
        #
        # It used to be the swarm's bytes for this run, unlabelled, and that
        # number stops short of 100% on a film that is entirely here: pieces
        # already on disk never have to be downloaded again, so a finished film
        # reads "83%" for ever and looks stuck. Reported from rpitv at 130
        # peers: "stuck at 97%", on a file that was complete and playing. The
        # speed and peers carry what the swarm is doing; the percentage has to
        # say what it is a percentage of.
        parts = []
        if self._verifying:
            # The hashing is still running beside the film, now that the port
            # opens before it finishes. Nothing is said at 0%: the film is
            # playing, and "verifying 0%" beside "0%" reads as two failures.
            parts.append("verifying %d%%" % self._verifying)
        info = self.served_file()
        if info is not None:
            parts.append("%d%% on disk" % info["percent"])
        elif self._progress["downloaded"]:
            # No file to measure yet: this run's bytes are all there is.
            parts.append("%d%% downloaded" % self._progress["percent"])
        parts.append("%s/s" % _size(self._progress["speed"]))
        parts.append("%d peers" % self._progress["peers"])
        text = " · ".join(parts)
        if info is not None and info["percent"] >= 99:
            # Complete on disk: no peers is the normal state, not a warning.
            quiet = 0
        elif info is None or self._progress["peers"]:
            # Nothing has come in for a while. Without a file to check -- while
            # the torrent is still being resolved -- the old clock is all there
            # is, and it is what this line always used.
            quiet = int(time.time() - self._moved_at) if self._moved_at is not None else 0
        else:
            quiet = self.stalled_for()
        if quiet >= _STALLED_AFTER:
            return text + " — nothing arriving for %ds" % quiet
        return text

    def served_file(self):
        """
        The file this play asked for: how big it is and how much is on disk.

        peerflix's /.json says how much *this run* has downloaded, which is 0
        for a film that was downloaded yesterday, and says nothing about the
        file on the card. The share that is really here is the difference
        between the file's logical size and what the filesystem has allocated
        for it: a torrent file is written as pieces arrive, and the parts that
        have not arrived are holes.

        None until the swarm has answered once.
        """
        files = [f for f in ((self._progress or {}).get("files") or []) if isinstance(f, dict)]
        if not files:
            cherrypy.log(
                "bf-disk: no file list from the swarm (progress has %d keys)"
                % len(self._progress or {})
            )
            # The file list can be missing even while the swarm is answering
            # -- peerflix only sends it once the metadata is parsed, and a
            # poll that lands before that has the totals and nothing else.
            # Fall back to what is on the card: for the single-file torrents
            # this box plays, the largest file under the buffer is the film.
            found = self._largest_buffered_file()
            if found is None:
                cherrypy.log("bf-disk: nothing found under " + BUFFER_DIR)
                return None
            cherrypy.log("bf-disk: measured " + found[0])
            total = int((self._progress or {}).get("total") or 0)
            present = found[1]
            return {
                "name": os.path.basename(found[0]),
                "path": found[0],
                "length": total or present,
                "present": present,
                "percent": min(100, int(present * 100 / total)) if total else 0,
            }
        # peerflix serves index 0 when no -i was given, so an unset index is
        # index 0 and not "unknown": this used to return nothing at all for a
        # magnet that carried no file index, which is most of them, and the one
        # reading that says how much of the film is on the card disappeared
        # exactly when a stall made it worth looking at.
        index = self.idx if isinstance(self.idx, int) else 0
        entry = files[index] if 0 <= index < len(files) else files[0]
        cherrypy.log("bf-disk: file list has %d entries, using %r" % (len(files), entry.get("name")))
        name = str(entry.get("name") or "")
        length = int(entry.get("length") or 0)
        path = self._resolve(name)
        present = 0
        if path is not None:
            try:
                st = os.stat(path)
            except OSError:
                path = None
            else:
                # Allocated, not logical: the holes are what is missing. It can
                # round past the end of the file, hence the clamp.
                present = min(st.st_blocks * 512, st.st_size)
                length = length or st.st_size
        percent = min(100, int(present * 100 / length)) if length else 0
        return {
            "name": name,
            "path": path,
            "length": length,
            "present": present,
            "percent": percent,
        }

    def _largest_buffered_file(self):
        """The biggest file in the buffer, with its allocated bytes, or None."""
        best = None
        try:
            for root, _dirs, names in os.walk(BUFFER_DIR):
                for name in names:
                    if name.startswith("."):
                        continue
                    path = os.path.join(root, name)
                    try:
                        st = os.stat(path)
                    except OSError:
                        continue
                    present = min(st.st_blocks * 512, st.st_size)
                    if best is None or present > best[1]:
                        best = (path, present)
        except OSError:
            return None
        return best

    def _resolve(self, name):
        """Where a torrent-relative file name is on disk, or None."""
        if not name:
            return None
        direct = os.path.join(BUFFER_DIR, name)
        if os.path.isfile(direct):
            return direct
        cached = getattr(self, "_path_cache", {}).get(name)
        if cached and os.path.isfile(cached):
            return cached
        # peerflix reports a file's name, which for a pack is relative to the
        # torrent, and the buffer holds one directory per torrent. Walk for it
        # rather than assume the shape, and remember where it was.
        base = os.path.basename(name)
        for root, _dirs, files in os.walk(BUFFER_DIR):
            if base in files:
                found = os.path.join(root, base)
                if not hasattr(self, "_path_cache"):
                    self._path_cache = {}
                self._path_cache[name] = found
                return found
        return None

    def _check_buffer_space(self, files):
        """
        The one space decision for this play, made when the film's size is known.

        peerflix reports the file list and each file's length from its metadata;
        the progress thread is the first place both are available. Free enough
        for the bytes still to be written plus the headroom, by removing whole
        buffer entries, least recently used first. Returns False while the size
        is still unknown, so the next poll tries again. Never raises into the
        progress loop.
        """
        index = self.idx if isinstance(self.idx, int) and self.idx >= 0 else 0
        entry = files[index] if index < len(files) else files[0]
        if not isinstance(entry, dict):
            return False
        length = int(entry.get("length") or 0)
        if not length:
            return False
        name = str(entry.get("name") or "").replace("\\", "/")
        current = os.path.normpath(name).split(os.sep)[0] if name else None
        if not current:
            return False
        info = self.served_file()
        present = int(info.get("present") or 0) if info else 0
        remaining = max(0, length - present)
        self._space_needed = remaining + buffer_space.HEADROOM
        try:
            freed, removed, free = buffer_space.reclaim(
                BUFFER_DIR, self._space_needed, current
            )
        except Exception as exc:
            # A space check is not worth taking the play down for.
            cherrypy.log("bf-space: could not check %s: %s" % (BUFFER_DIR, exc))
            return True
        self._space_freed = freed
        self._space_removed = removed
        if removed:
            cherrypy.log(
                "bf-space: needed %d MB, freed %d MB by removing %d entr%s"
                % (
                    self._space_needed // (1024 * 1024),
                    freed // (1024 * 1024),
                    len(removed),
                    "y" if len(removed) == 1 else "ies",
                )
            )
        if free + freed < self._space_needed:
            cherrypy.log(
                "bf-space: still short after removing %d: %d MB free, %d MB needed"
                % (
                    len(removed),
                    (free + freed) // (1024 * 1024),
                    self._space_needed // (1024 * 1024),
                )
            )
        return True

    def stalled_for(self):
        """
        How long this torrent has been unable to bring anything new, in seconds.

        Zero unless it matters: a film that is complete on disk needs no peers
        and must never be reported as stuck, and one that has peers is working
        even if it is slow. What is left is the real case -- pieces still
        missing and nobody to ask -- which is what the freeze looks like from
        the source's side, minutes before the picture notices.
        """
        info = self.served_file()
        if not info or info["percent"] >= 99:
            return 0
        if not self._progress or self._progress["peers"]:
            return 0
        since = self._peers_seen_at or self._moved_at or self._started_at
        if since is None:
            return 0
        return max(0, int(time.time() - since))

    def debug(self):
        base = super().debug()
        swarm = {k: v for k, v in (self._progress or {}).items() if k != "files"}
        try:
            buffered = sum(len(names) for _root, _dirs, names in os.walk(BUFFER_DIR))
        except OSError:
            buffered = -1
        try:
            st = os.statvfs(BUFFER_DIR)
            free = st.f_bavail * st.f_frsize
        except OSError:
            free = -1
        base.update(
            {
                "swarm": swarm,
                "buffer": {
                    "dir": BUFFER_DIR,
                    "files": buffered,
                    "free": free,
                    "needed": self._space_needed,
                    "evicted": self._space_freed,
                    "removed": len(self._space_removed),
                },
                "file": self.served_file(),
                "stalled_for": self.stalled_for(),
                "verifying": self._verifying,
                "cache_hit": self._cache_hit,
                "serve_after": getattr(self, "_ready_after", None),
            }
        )
        return base

    def _watch_progress(self):
        """
        Ask the swarm what it is doing, for as long as the torrent is open.

        peerflix answers /.json with the piece of its own bookkeeping this
        needs: bytes downloaded against the torrent's length, the speed, the
        peers, and the files it is serving. Cheap, one local request every two
        seconds.

        It used to stop after five minutes, on the grounds that a film that is
        playing says what it is doing on the screen. What it does not say is
        when it has stopped being able to: a swarm that has gone to zero peers
        looks exactly like a film, right up until the buffer runs out. The
        panel and the stall warning both need this while the film plays, so it
        now runs until the stage is stopped.
        """
        base = (self.args.get("outfile") or "").rstrip("/")
        if not base:
            return
        ticks = 0
        while not self.killing:
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
            files = stats.get("files") or []
            if not total:
                time.sleep(_PROGRESS_EVERY)
                continue
            percent = int(downloaded * 100 / total)
            # Bytes, not percent: one percent of a 3.2GB film is 32MB, so on a
            # slow swarm the rounded figure stands still for a minute while
            # data is plainly arriving, and the bar warned "nothing arriving"
            # beside a 500 kB/s figure.
            if self._progress is None or downloaded != self._progress["downloaded"]:
                self._moved_at = time.time()
            self._progress = {
                "downloaded": downloaded,
                "percent": percent,
                "total": total,
                "speed": int(stats.get("downloadSpeed") or 0),
                "uploaded": int(stats.get("uploaded") or 0),
                "upload_speed": int(stats.get("uploadSpeed") or 0),
                "peers": int(stats.get("totalPeers") or 0),
                "active_peers": int(stats.get("activePeers") or 0),
                "files": files,
            }
            if self._progress["peers"]:
                self._peers_seen_at = time.time()
            self._progress_at = time.time()
            if not self._space_checked and files:
                self._space_checked = self._check_buffer_space(files)
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

    def _watch_verify(self, line):
        """
        Follow the hashing for the whole run, and evict as it finishes.

        The port opens before the hashing is done, so `_ready()` returns at
        "server is listening" and never sees the percentages that follow. The
        copier still feeds every line here, which is where they have to be
        read: without this the panel held "verify 0%" for a verification that
        had in fact reached 100 and saved its flag.

        peerflix prints the percentage of the *latest verified piece*, not a
        count of pieces hashed by the verification loop. The swarm also emits
        'verify' for pieces it downloads, and those can be behind the hashing
        front -- so after 100 the number can print 99 again. Keep the highest
        seen, and once 100 is reached the hashing is over for this play: a
        later 99 must not bring "verifying" back.

        Eviction: verification reads gigabytes through the page cache and the
        decoder sets up on the first frame, which arrives with the file's tail
        -- the cues live at its end. A drop only at the finish comes too late
        for a setup attempted on the tail: measured on rpi, twelve "Atomic
        commit failed" six minutes after 99% with the region never cleared in
        between. So the drop fires at 90, clearing the region before the tail
        is hashed, and again at 99. The percentage rounds, so both appear for
        any torrent with more than a few hundred pieces; slightly early is
        still the cleared region, since nothing re-pollutes afterwards except
        the swarm's own downloads, which are megabytes.
        """
        if line.startswith("bf-verify-cache: hit"):
            # The flag was taken and nothing was hashed; the line can arrive
            # after the port opened, so `_ready()` may not be the one to see it.
            self._cache_hit = True
            self._verifying = None
            return
        found = _VERIFYING_RE.search(line)
        if not found:
            return
        percent = int(found.group(1))
        if self._verified is None or percent > self._verified:
            self._verified = percent
        self._verifying = None if self._verified >= 100 else self._verified
        if self.killing:
            return
        for mark in (90, 99):
            if percent >= mark and mark not in self._drop_marks:
                self._drop_marks.add(mark)
                dropped = drop_buffer_cache()
                cherrypy.log(
                    "evicted %d buffer files at verifying %d%%" % (dropped, percent)
                )

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

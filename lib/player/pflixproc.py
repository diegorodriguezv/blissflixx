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


class PeerflixProcess(ExternalProcess):
    def __init__(self, torrent, idx, subs=None):
        super().__init__()
        #: The last "Verifying downloaded: N%" seen, or None.
        self._verifying = None
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
            if line.startswith("Bad Response"):
                raise ProcessException(line)
            # Get this error if site down/blocked
            # html page instead of torrent
            elif line.startswith("not a colon at"):
                raise ProcessException("Unable to retrieve torrent")
            elif line.startswith("server is listening"):
                self.args["outfile"] = self._advertised_url(line)
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
        text = "%d%% · %s/s · %d peers" % (
            self._progress["percent"],
            _size(self._progress["speed"]),
            self._progress["peers"],
        )
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
        while time.time() < deadline:
            if self.killing:
                return
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


def _size(value):
    """Bytes per second, in words."""
    if value >= 1024 * 1024:
        return "%.1f MB" % (value / (1024 * 1024))
    if value >= 1024:
        return "%d kB" % (value / 1024)
    return "%d B" % value

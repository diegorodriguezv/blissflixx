import os
import re

import cherrypy

from ..api.torrent import torrent2magnet
from ..locations import DATA_PATH
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


class PeerflixProcess(ExternalProcess):
    def __init__(self, torrent, idx):
        super().__init__()
        cmd = ["node", "--max-old-space-size=128", "/usr/local/bin/peerflix"]
        # Avoid problems with downloading torrent files
        torrent = torrent2magnet(torrent)
        cmd.append(torrent)
        cmd.append("-q")
        # -r used to be passed here. peerflix spells it --remove, "remove files
        # on exit", and it meant that every torrent started again from nothing:
        # the download could not survive being stopped, and someone with poor
        # peers or a poor connection lost the whole of their progress on every
        # attempt. Nothing deletes torrent data here now.
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
        while True:
            line = self._readline()
            if line.startswith("Bad Response"):
                raise ProcessException(line)
            # Get this error if site down/blocked
            # html page instead of torrent
            elif line.startswith("not a colon at"):
                raise ProcessException("Unable to retrieve torrent")
            elif line.startswith("server is listening"):
                self.args["outfile"] = self._advertised_url(line)
                return self.args

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

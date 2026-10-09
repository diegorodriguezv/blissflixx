import re
import shutil

import cherrypy

from ..api.torrent import torrent2magnet
from .processpipe import ExternalProcess, ProcessException

_PEERFLIX_PORT = "9696"

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
        cmd.append("-r")
        cmd.append("-p")
        cmd.append(_PEERFLIX_PORT)
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
        try:
            shutil.rmtree("/tmp/torrent-stream")
        except Exception:
            pass
        super().stop()

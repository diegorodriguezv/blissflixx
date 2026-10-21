import os
import re

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

#: How long to wait for a subtitle file out of the torrent. It is a few KB and
#: the swarm is already running, so this is generous; past it the film plays
#: without subtitles rather than not at all.
_SUBS_FETCH_TIMEOUT = 20


class PeerflixProcess(ExternalProcess):
    def __init__(self, torrent, idx, subs_idx=None, subs_name=None):
        super().__init__()
        #: The file inside this torrent to pull out as subtitles, and what to
        #: call it here. None when this play asked for none.
        self.subs_idx = subs_idx
        self.subs_name = subs_name
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
                if self.subs_idx is not None:
                    path = self._fetch_subtitle(self.args["outfile"])
                    if path is not None:
                        self.args["subtitles"] = path
                return self.args

    def _fetch_subtitle(self, base_url):
        """
        Take the subtitle file out of the torrent being played.

        peerflix serves the whole torrent over one port: "/" is the file that
        was selected and "/12" is the twelfth, so a subtitle comes from the
        swarm that is already running for a few KB, instead of a second
        instance announcing all over again for a file that is a hundredth of
        the size.

        Never fatal. A film without subtitles is still a film, and a torre... a
        source whose subtitle pieces are slow to arrive is still worth playing.
        """
        url = base_url.rstrip("/") + "/" + str(self.subs_idx)
        name = os.path.basename(self.subs_name or "subtitle-" + str(self.subs_idx))
        name = re.sub(r"[^\w.\-]", "_", name)
        path = os.path.join(TMP_DIR, name)
        try:
            r = requests.get(url, timeout=_SUBS_FETCH_TIMEOUT)
            if r.status_code >= 300:
                raise ValueError("HTTP " + str(r.status_code))
            if not r.content:
                raise ValueError("empty reply")
            with open(path, "wb") as handle:
                handle.write(r.content)
        except Exception as exc:
            cherrypy.log("could not fetch the subtitle %s: %s" % (url, exc))
            return None
        cherrypy.log("subtitle saved: " + path)
        return path

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

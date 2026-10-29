"""The torrent source, backed by libtorrent.

This is the stage that turns a magnet into something a player can open. It
starts bin/bt_serve.py -- a separate process, under the system python, because
python3-libtorrent is a system package and the application runs in a virtualenv
-- which prints one line when it can serve, a status line every couple of
seconds, and serves the file over HTTP.

What it is for, measured on a Pi 3 cold against a 1.9GB torrent: metadata in
1.9-4.3s, 41-49 peers within 30s, 1.65-1.78MB/s. More to the point, it fetches
what the reader asks for: a play 60 pieces in had downloaded 120MB of that file,
a seek cost the pieces it landed in, and an episode played out of a 4GB season
pack fetched only that episode and not the other nine.

The bar reads the same fields it always has -- the share of the file that is on
the card, the rate, the peers, and how long nothing has arrived -- so its
meaning did not change when this replaced the old node-based source.
"""

import json
import os
import re
import subprocess
import time

import cherrypy

from ..api.torrent import torrent2magnet
from . import buffer as buffer_space
from .buffer import BUFFER_DIR
from .processpipe import ExternalProcess, ProcessException

#: Where bt_serve.py listens.
_PORT = "9697"

#: The system interpreter, not the virtualenv: libtorrent comes from the
#: python3-libtorrent package. configure.sh installs it; a box without it says
#: so in one line, with the command that fixes it.
SYSTEM_PYTHON = "/usr/bin/python3"

BT_SERVE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bin",
    "bt_serve.py",
)

_SERVING_RE = re.compile(
    r"bf-bt: serving (http://\S+) index=(-?\d+) name=(.*?) length=(\d+) "
    r"pieces=(\d+) top=(.*)$"
)

_STATUS_PREFIX = "bf-status: "
_FILES_PREFIX = "bf-files: "
_ERROR_PREFIX = "bf-bt: error "

#: How long the numbers may stay identical before the bar says nothing is
#: arriving. Long enough that one slow piece does not read as a dead torrent,
#: short enough that a dead one is not left looking busy.
_STALLED_AFTER = 20


def _size(value):
    """Bytes per second, in words."""
    if value >= 1024 * 1024:
        return "%.1f MB" % (value / (1024 * 1024))
    if value >= 1024:
        return "%d kB" % (value / 1024)
    return "%d B" % value


def list_files(link, timeout=90):
    """
    The files inside a torrent, as (path, size) in the torrent's own order.

    This runs the same torrent process in its listing mode: same trackers, same
    saved .torrent, same timeout and same error. Nothing is downloaded and no
    port is opened. Until this existed the only way to list a torrent was the
    old source, which is why that source could not be removed.
    """
    cmd = [
        SYSTEM_PYTHON,
        BT_SERVE,
        torrent2magnet(link),
        "--dir",
        BUFFER_DIR,
        "--list",
        "--metadata-timeout",
        "45",
    ]
    try:
        done = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "No answer listing this torrent in %ds | %s"
            % (timeout, " ".join(str(part) for part in cmd))
        )
    text = (done.stdout or b"").decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.startswith(_FILES_PREFIX):
            continue
        try:
            files = json.loads(line[len(_FILES_PREFIX):])
            return [(str(f["path"]), int(f["size"])) for f in files]
        except (ValueError, KeyError, TypeError):
            break
    tail = " | ".join(text.strip().splitlines()[-3:])
    raise RuntimeError(
        "Unable to retrieve torrent files | %s | %s"
        % (" ".join(str(part) for part in cmd), tail)
    )


class LibtorrentProcess(ExternalProcess):
    is_source = True

    def __init__(self, torrent, idx, subs=None):
        super().__init__()
        #: The last status line from bt_serve, already parsed.
        self._status = None
        #: When the payload last changed, for the stalled line.
        self._moved_at = None
        self._started_at = None
        #: The files inside this torrent to serve as subtitles, most probable
        #: first: [{"idx": n, "name": ".."}]. Empty when this play asked for
        #: none.
        self.subs = subs or []
        #: Which file inside the torrent this play asked for.
        self.idx = idx if isinstance(idx, int) and idx >= 0 else 0
        #: The buffer entry this film occupies, from the serving line. The space
        #: check must never delete the entry being played.
        self._top = None
        #: The one space decision for this play, made when the film's size and
        #: what is already on disk are both known. In memory only; the panel
        #: reads it and the log records it.
        self._space_checked = False
        self._space_needed = 0
        self._space_freed = 0
        self._space_removed = []
        #: Every line is offered to _watch_status: the status lines arrive
        #: after the port opens, which is when _ready() has already returned.
        self.on_output_line = self._watch_status
        cmd = [
            SYSTEM_PYTHON,
            BT_SERVE,
            torrent2magnet(torrent),
            "--dir",
            BUFFER_DIR,
            "--port",
            _PORT,
            "--index",
            str(self.idx),
        ]
        wanted = [s["idx"] for s in self.subs if isinstance(s.get("idx"), int)]
        if wanted:
            cmd += ["--subs", ",".join(str(i) for i in wanted)]
        self.cmd = cmd

    def name(self):
        return "libtorrent"

    def _get_cmd(self, args):
        self.args = args
        return self.cmd

    def _ready(self):
        self._started_at = time.time()
        try:
            return self._read_until_serving()
        except ProcessException as exc:
            # Every failure here is one the person cannot fix from the screen,
            # and the rule for this application is that such a message carries
            # the command that reproduces it -- including a crash, where the
            # only useful thing said is the exit status.
            raise ProcessException(
                "%s | %s" % (exc, " ".join(str(p) for p in self.cmd))
            )

    def _read_until_serving(self):
        while True:
            line = self._readline()
            if line.startswith(_ERROR_PREFIX):
                raise ProcessException(line[len(_ERROR_PREFIX):])
            serving = _SERVING_RE.search(line)
            if serving:
                self.args["outfile"] = serving.group(1).rstrip("/")
                info = {
                    "index": int(serving.group(2)),
                    "name": serving.group(3),
                    "length": int(serving.group(4)),
                    "pieces": int(serving.group(5)),
                }
                self._top = serving.group(6).strip()
                cherrypy.log(
                    "TIMING libtorrent %.1fs to serve (%s, %d pieces)"
                    % (time.time() - self._started_at, info["name"], info["pieces"])
                )
                if self.subs:
                    # Where the subtitles live, not the subtitles: fetching one
                    # waits on pieces that may not be here yet, and waiting here
                    # delays the film. The player fetches them in the background
                    # and adds them mid-play. See VlcProcess._add_torrent_subtitles.
                    base = self.args["outfile"]
                    self.args["subtitles_wanted"] = [
                        {
                            "url": base + "/" + str(s["idx"]),
                            "name": s.get("name") or "subtitle-" + str(s["idx"]),
                        }
                        for s in self.subs
                    ]
                return self.args

    def _watch_status(self, line):
        """Remember the latest numbers the torrent process printed."""
        if not line.startswith(_STATUS_PREFIX):
            return
        try:
            stats = json.loads(line[len(_STATUS_PREFIX):])
        except ValueError:
            return
        if not isinstance(stats, dict):
            return
        if self._status is not None and stats.get("downloaded") != self._status.get("downloaded"):
            self._moved_at = time.time()
        self._status = stats
        if not self._space_checked:
            self._space_checked = self._check_buffer_space()

    def _check_buffer_space(self):
        """
        The one space decision for this play, made when the size is known.

        The rule is the buffer module's: free the bytes still to be written plus
        the headroom the app leaves for the system, by removing whole buffer
        entries, least recently used first. The film being played is never a
        candidate. Returns False while the numbers are not here yet, so the next
        status line tries again; never raises into the status path.
        """
        info = self.served_file()
        if not info or not info["length"] or not self._top:
            return False
        remaining = max(0, info["length"] - info["present"])
        self._space_needed = remaining + buffer_space.HEADROOM
        try:
            freed, removed, free = buffer_space.reclaim(
                BUFFER_DIR, self._space_needed, self._top
            )
        except Exception as exc:
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

    def status_msg(self):
        """
        What the bar says before the picture: the swarm's side of the wait.

        Before the torrent has said anything at all it is a plain loading line;
        once it has, the state is worth naming, because "waiting for peers" is
        the case that can last and needs the person to see it.
        """
        if self._status is None:
            return "LOADING STREAM"
        if self._status.get("state") == "metadata":
            return "LOOKING FOR PEERS"
        return "LOADING STREAM"

    def served_file(self):
        """
        The file this play asked for: how big it is and how much is on disk.

        libtorrent counts this per file while it downloads, so the share is
        exact rather than inferred from what the filesystem has allocated. None
        until the torrent has said anything.
        """
        if not self._status:
            return None
        files = self._status.get("files") or []
        if not files:
            return None
        index = self._status.get("playing") or 0
        entry = files[index] if 0 <= index < len(files) else files[0]
        return {
            "name": entry.get("name") or "",
            "length": int(entry.get("length") or 0),
            "present": int(entry.get("present") or 0),
            "percent": int(entry.get("percent") or 0),
        }

    def detail(self):
        """
        The torrent's numbers, while the picture has not started.

        The reading the bar has always used: the share that is on disk, said as
        such, then the rate and the peers. A film that is entirely here says
        100% and no peers, which is the truth rather than a warning.
        """
        if not self._status:
            return ""
        state = self._status.get("state")
        peers = self._status.get("peers") or 0
        if state == "metadata":
            # The film's own numbers do not exist yet; what is worth saying is
            # whether anyone has answered at all.
            text = "%d peer%s" % (peers, "" if peers == 1 else "s")
        else:
            info = self.served_file()
            parts = []
            if info is not None:
                parts.append("%d%% on disk" % info["percent"])
            parts.append("%s/s" % _size(self._status.get("speed") or 0))
            parts.append("%d peer%s" % (peers, "" if peers == 1 else "s"))
            text = " · ".join(parts)
        quiet = self.stalled_for()
        if quiet >= _STALLED_AFTER:
            return text + " — nothing arriving for %ds" % quiet
        return text

    def stalled_for(self):
        """
        How long this torrent has been unable to bring anything new, in seconds.

        Zero unless it matters: a film complete on disk needs no peers and must
        never be reported as stuck, and one with peers is working even if it is
        slow. What is left is the real case -- pieces still missing and nobody
        to ask -- which the bar has to show before the picture stops.
        """
        info = self.served_file()
        if not self._status:
            return 0
        if self._status.get("checking") is not None:
            # A hash check is work, and it is the card being read, not a swarm
            # with nothing to send. It must not read as "nothing arriving".
            return 0
        if info and info["percent"] >= 99:
            return 0
        if self._status.get("peers"):
            return 0
        since = self._moved_at or self._started_at
        if since is None:
            return 0
        return max(0, int(time.time() - since))

    def debug(self):
        """
        The panel's sections, in the shape it reads them.

        The diagnostics panel reads swarm.speed, swarm.peers and
        swarm.active_peers, and buffer.needed/evicted/removed/free; naming them
        differently here would leave the panel blank rather than wrong.
        """
        base = super().debug()
        swarm = {
            k: v for k, v in (self._status or {}).items() if k != "files"
        }
        try:
            buffered = sum(len(names) for _root, _dirs, names in os.walk(BUFFER_DIR))
            free = buffer_space.free_bytes(BUFFER_DIR)
        except OSError:
            buffered, free = -1, -1
        base.update(
            {
                "swarm": swarm,
                "file": self.served_file(),
                "stalled_for": self.stalled_for(),
                # The panel's own lines: how long this stage took to serve,
                # and whether a hash check is running. Without these it showed
                # "—" and "not running" for every libtorrent play.
                "serve_after": getattr(self, "_ready_after", None),
                "verifying": (self._status or {}).get("checking"),
                "buffer": {
                    "dir": BUFFER_DIR,
                    "files": buffered,
                    "free": free,
                    "needed": self._space_needed,
                    "evicted": self._space_freed,
                    "removed": len(self._space_removed),
                },
                "command": " ".join(str(p) for p in self.cmd),
            }
        )
        return base

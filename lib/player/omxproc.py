import os
import time

from ..locations import BIN_PATH
from .backend import CAP_PAUSE, CAP_STOP, CAP_SUBTITLES, OmxplayerBackend
from .processpipe import ProcessException

OMX_CMD = "omxplayer --timeout 120 -I --no-keys "
_DBUS_PATH = os.path.join(BIN_PATH, "dbus.sh")
_INPUT_TIMEOUT = 10
_START_TIMEOUT = 120


class OmxplayerProcess(OmxplayerBackend):
    """
    Plain omxplayer, controlled over dbus.

    Kept for the case where the media is already a plain stream and no
    downstream process needs to feed it.
    """

    #: dbus can toggle playback and nothing else here.
    capabilities = frozenset({CAP_PAUSE, CAP_SUBTITLES})

    start_timeout = _START_TIMEOUT

    def __init__(self):
        super().__init__(shell=True)

    def build_command(self, args):
        cmd = OMX_CMD
        if "subtitles" in args:
            cmd = cmd + "--align center --subtitles '" + args["subtitles"] + "' "
        fname = args["outfile"]
        if fname.startswith("http"):
            return cmd + "'" + fname + "'"
        # A local file is still being written by the download stage, so playback
        # is piped from tail, starting past the bytes already on disk. The pid
        # tells tail when the producer exits; yt-dlp is what supplies it.
        pid = args.get("pid")
        if pid is None:
            raise ProcessException(
                "omxplayer needs the producing process id to tail a local file"
            )
        tail = "tail -f --pid=" + str(pid) + ' --bytes=+0 "' + fname + '"'
        return tail + " | " + cmd + "pipe:0"

    def name(self):
        return "omxplayer"

    def _wait_input(self, fname):
        for i in range(_INPUT_TIMEOUT):
            if os.path.isfile(fname):
                return True
            time.sleep(1)
        return False

    def start(self, args):
        fname = args["outfile"]
        if not fname.startswith("http"):
            # Nothing to play until the download has created the file.
            if not self._wait_input(fname):
                self._set_error("Omxplayer timed out waiting for input file")
                self.msg_halted()
                return
            # Give the tail a moment to attach before the player starts.
            time.sleep(5)
        super().start(args)

    def control(self, action):
        dbcmd = None
        if action == "pause" or action == "resume":
            dbcmd = "pause"
        if dbcmd is not None:
            os.system(_DBUS_PATH + " " + dbcmd + " &")

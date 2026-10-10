import os

from .backend import ALL_CAPABILITIES, CAP_PAUSE, CAP_STOP, OmxplayerBackend

# timeout for network connections in seconds (3 retries), 0 means no timeout
OMX_CMD = "omxplayer.bin --timeout 0 -I "
# timeout for the first line of text from omxplayer in seconds, None means no timeout
_START_TIMEOUT = None
# path to the fifo por IPC
_CMD_FIFO = "/tmp/blissflixx/cmdfifo"


class OmxplayerProcess2(OmxplayerBackend):
    """
    omxplayer.bin reading keystrokes from a FIFO.

    This is the variant used whenever control matters: dlsrv serves a file that
    is still growing, so playback is driven by writing keys to stdin rather than
    by dbus messages.
    """

    #: This variant implements the whole action set.
    capabilities = ALL_CAPABILITIES

    # The binary is .bin rather than omxplayer because the wrapper emits dbus
    # calls that made the process "suddenly die"; see commit 6d21a23. --timeout 0
    # means never give up waiting for input, which is right here because the
    # file being read is still growing and has no end.
    defaults = {
        "binary": "omxplayer.bin",
        "extra_args": ["--timeout", "0", "-I"],
        "start_timeout": _START_TIMEOUT,
        "fifo": _CMD_FIFO,
    }

    def __init__(self, config=None):
        super().__init__(config=config)
        self.shell = True

    @property
    def start_timeout(self):
        return self.opt("start_timeout")

    @property
    def fifo(self):
        return self.opt("fifo")

    def build_command(self, args):
        cmd = self.opt("binary") + " " + " ".join(self.opt("extra_args"))
        if "subtitles" in args:
            cmd += " --align center --subtitles '" + args["subtitles"] + "'"
        return "tail -f " + self.fifo + " | " + cmd + " '" + args["outfile"] + "'"

    def name(self):
        return "omxplayer with keys"

    def start(self, args):
        if not os.path.exists(self.fifo):
            os.system("mkfifo " + self.fifo)
        self.control("show_subtitle")
        super().start(args)

    def stop(self):
        if os.path.exists(self.fifo):
            try:
                os.remove(self.fifo)
            except Exception:
                pass
        super().stop()

    def _send_key(self, key):
        os.system("echo -n " + key + " >> " + self.fifo + " &")

    def control(self, action):
        key = None
        if action == "pause" or action == "resume":
            key = "p"
        elif action == "stop":
            key = "q"
        elif action == "subminus":
            key = "d"
        elif action == "subplus":
            key = "f"
        elif action == "plus600":
            key = "$'\x1b\x5b\x41'"
        elif action == "minus600":
            key = "$'\x1b\x5b\x42'"
        elif action == "plus30":
            key = "$'\x1b\x5b\x43'"
        elif action == "minus30":
            key = "$'\x1b\x5b\x44'"
        elif action == "volup":
            key = "="
        elif action == "voldown":
            key = "-"
        elif action == "next_subtitle":
            key = "m"
        elif action == "prev_subtitle":
            key = "n"
        elif action == "next_audio":
            key = "k"
        elif action == "prev_audio":
            key = "j"
        elif action == "show_subtitle":
            key = "w"
        elif action == "hide_subtitle":
            key = "x"
        if key is not None:
            self._send_key(key)

"""
MpV backend.

The first backend that is not an omxplayer, and the reason the abstraction is
worth having: it renders the same media, accepts the same control actions, and
shares none of omxplayer's mechanics.

Two things differ from the omxplayer backends:

- No status output. mpv run with --no-terminal and --msg-level=all=error says
  nothing until something goes wrong, so readiness cannot be read from a line
  stream the way OmxplayerBackend does. Instead the IPC socket is polled for:
  mpv binds it during startup, so its appearance means mpv is up. Errors that
  arrive first are surfaced from the output stream.
- Control goes over a unix socket as JSON IPC rather than as dbus messages or
  FIFO keystrokes. That is a property list rather than keystrokes, so actions map
  to commands directly and unknown ones can be reported instead of dropped.

mpv is not installed by configure.sh; it is here so the backend can be selected
and so the abstraction is exercised by an implementation that shares nothing
with omxplayer.
"""

import json
import os
import socket
import time

from .backend import (
    ALL_CAPABILITIES,
    CAP_AUDIO_TRACK,
    CAP_PAUSE,
    CAP_SEEK,
    CAP_STOP,
    CAP_SUBTITLES,
    CAP_VOLUME,
    PlayerBackend,
)
from .processpipe import ProcessException

MPV_BIN = "mpv"
_SOCKET_PATH = "/tmp/blissflixx/mpv.sock"
#: Seconds to wait for mpv to bind the IPC socket.
_START_TIMEOUT = 30

#: Output patterns that mean mpv will not play this input.
_ERROR_MARKERS = (
    "Failed to open",
    "error parsing option",
    "No such file or directory",
    "Failed to initialize",
)


class MpvProcess(PlayerBackend):
    """
    Renders via mpv.

    mpv takes argv rather than a shell string, so this backend needs no shell and
    no FIFO. It can read a growing file, an http url, or a pipe, all directly.

    The default flags are the invocation verified working on the Pi: DRM/KMS
    presentation through the GPU context, no hardware video decoding, and audio
    routed to the HDMI card. Those last two are the parts that differ between
    models, so audio_device is configurable rather than baked in.
    """

    capabilities = ALL_CAPABILITIES

    defaults = {
        "binary": MPV_BIN,
        # --no-terminal: stdout and stderr are this process's status stream and
        #   _ready reads it.
        # --no-config: keep whatever mpv.conf the box happens to have out of
        #   playback. --profile=fast still applies; it is a builtin profile.
        # --vo/--gpu-context/--gpu-api: DRM presentation on the Pi.
        # --hwdec=no: measured faster than hardware decode for this content.
        "extra_args": [
            "--no-terminal",
            "--no-config",
            "--profile=fast",
            "--vo=gpu",
            "--gpu-context=drm",
            "--gpu-api=opengl",
            "--hwdec=no",
        ],
        "audio_device": "alsa/hdmi:CARD=vc4hdmi,DEV=0",
        "socket": _SOCKET_PATH,
        "start_timeout": _START_TIMEOUT,
    }

    def __init__(self, config=None):
        # shell=False: argv with no shell metacharacters.
        super().__init__(config=config)
        self.shell = False

    @property
    def socket_path(self):
        return self.opt("socket")

    @property
    def start_timeout(self):
        return self.opt("start_timeout")

    def build_command(self, args):
        cmd = [self.opt("binary")]
        cmd += list(self.opt("extra_args"))
        # Bind the control socket. mpv creates it while starting up, which is
        # what readiness is detected from.
        cmd.append("--input-ipc-server=" + self.socket_path)
        audio_device = self.opt("audio_device")
        if audio_device:
            cmd.append("--audio-device=" + audio_device)
        cmd += ["--idle=no", "--keep-open=no"]
        if "subtitles" in args:
            cmd.append("--sub-file=" + args["subtitles"])
        outfile = args["outfile"]
        if not outfile.startswith("http"):
            # A local file that is still being written: mpv waits for more data
            # rather than stopping at the current end of file.
            cmd.append("--keep-open=inf")
        cmd.append(outfile)
        return cmd

    def name(self):
        return "mpv"

    def _ready(self):
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if os.path.exists(self.socket_path):
                return
            if self.proc is not None and self.proc.poll() is not None:
                # mpv exited before binding; report whatever it said on the way.
                raise ProcessException(
                    self._drain_error() or "mpv exited during startup"
                )
            time.sleep(0.1)
        raise ProcessException("mpv timed out starting up")

    def _drain_error(self):
        """Return the first error line mpv printed, or None."""
        while True:
            line = self._readline(1)
            if not line:
                return None
            if any(marker in line for marker in _ERROR_MARKERS):
                return line

    # -- control ----------------------------------------------------------

    def _send_command(self, command):
        """Write one JSON IPC command to the control socket."""
        if not os.path.exists(self.socket_path):
            # Not running, or not ready yet. Nothing to control.
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(self.socket_path)
                sock.sendall(json.dumps({"command": command}).encode("utf-8"))
            return True
        except OSError:
            return False

    def _set_property(self, name, value):
        return self._send_command(["set_property", name, value])

    def _cycle(self, name):
        return self._send_command(["cycle", name])

    def _seek(self, seconds):
        return self._send_command(["seek", seconds])

    def _set_volume(self, delta):
        return self._send_command(["add", "volume", delta])

    def control(self, action):
        if action in ("pause", "resume"):
            self._set_property("pause", action == "pause")
        elif action == "stop":
            self._send_command(["quit"])
        elif action == "plus30":
            self._seek(30)
        elif action == "minus30":
            self._seek(-30)
        elif action == "plus600":
            self._seek(600)
        elif action == "minus600":
            self._seek(-600)
        elif action == "volup":
            self._set_volume(5)
        elif action == "voldown":
            self._set_volume(-5)
        elif action in ("next_subtitle", "prev_subtitle"):
            self._cycle("sub")
        elif action in ("show_subtitle", "hide_subtitle"):
            self._set_property("sub-visibility", action == "show_subtitle")
        elif action in ("next_audio", "prev_audio"):
            self._cycle("audio")

    def stop(self):
        if os.path.exists(self.socket_path):
            try:
                os.remove(self.socket_path)
            except OSError:
                pass
        super().stop()

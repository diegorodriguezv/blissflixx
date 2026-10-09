"""
VLC backend.

VLC is the recommended player on current Raspberry Pi OS. Hardware H.264 decode,
HDMI audio and subtitles all work without the expensive GStreamer compositing
path, which makes it the most capable of the three non-omxplayer options.

Two things differ from the mpv backend:

- Control goes over VLC's rc interface as plaintext commands on a unix socket.
  A socket rather than rc-host because it needs no port, so there is nothing to
  collide with and no password to configure. The commands are a small verb set,
  which suits it: pause, play, stop, seek, volup, voldown, quit.
- Readiness is detected the same way as for mpv, by polling for the control
  socket to appear. cvlc says very little on stdout with --intf=dummy, so there
  is no status line stream to parse the way omxplayer has.

Not implemented: audio track cycling. rc has no command for it, so
next_audio/prev_audio are declared absent rather than silently dropped.
"""

import os
import socket
import time

import cherrypy

from .backend import (
    CAP_PAUSE,
    CAP_SEEK,
    CAP_STOP,
    CAP_SUBTITLES,
    CAP_VOLUME,
    PlayerBackend,
)
from .processpipe import ProcessException

VLC_BIN = "cvlc"
_RC_SOCKET = "/tmp/blissflixx/vlc.sock"
_START_TIMEOUT = 30
#: Seconds to wait for VLC to acknowledge a command before giving up on a reply.
#: The command has already been written at that point, so this only bounds how
#: long the request thread can be held.
_REPLY_TIMEOUT = 2
#: Cap on the reply we read, so a chatty status dump cannot be pulled whole into
#: memory or the log.
_REPLY_MAX_BYTES = 4096
_REPLY_MAX_CHARS = 200

#: Output that means cvlc will not play this input.
_ERROR_MARKERS = (
    "cannot open",
    "no suitable decoder",
    "Your input can't be opened",
    "codec not supported",
)


class VlcProcess(PlayerBackend):
    """
    Renders via cvlc.

    cvlc takes argv, so no shell is needed. Unlike omxplayer it can read a
    growing http url, so it works with or without the dlsrv stage.
    """

    capabilities = frozenset({CAP_PAUSE, CAP_STOP, CAP_SEEK, CAP_VOLUME, CAP_SUBTITLES})

    # The defaults are the invocation measured working on the Pi: DRM/KMS video
    # output through the vc4 driver, ALSA audio pinned to the HDMI card, and no
    # interactive interface since control arrives over the rc socket.
    defaults = {
        "binary": VLC_BIN,
        "extra_args": ["--intf=dummy"],
        "audio_device": "hdmi:CARD=vc4hdmi,DEV=0",
        "video_output": "drm_vout",
        "video_output_module": "vc4",
        # The Pi's screen is small, so VLC's default text size is too small to
        # read at typical viewing distance.
        "subtitle_text_scale": "60",
        "rc_socket": _RC_SOCKET,
        "start_timeout": _START_TIMEOUT,
        "volume_step": 5,
        "volume_max": 512,
    }

    def __init__(self, config=None):
        super().__init__(config=config)
        self.shell = False
        # rc has no command that reports the current volume, so it is tracked
        # from the last set point. VLC treats 256 of 512 as its default level.
        self._volume = 256

    @property
    def rc_socket_path(self):
        return self.opt("rc_socket")

    @property
    def start_timeout(self):
        return self.opt("start_timeout")

    def build_command(self, args):
        cmd = [self.opt("binary")]
        cmd += list(self.opt("extra_args"))
        cmd += [
            "--aout=alsa",
            "--alsa-audio-device=" + self.opt("audio_device"),
            "--vout=" + self.opt("video_output"),
            "--drm-vout-module=" + self.opt("video_output_module"),
            "--sub-text-scale=" + self.opt("subtitle_text_scale"),
            # Accept control commands on a unix socket rather than a TCP port.
            "--extraintf=rc",
            "--rc-unix=" + self.rc_socket_path,
            # A run that must not stop on its own.
            "--no-video-title-show",
            "--play-and-exit",
        ]
        if "subtitles" in args:
            cmd.append("--sub-file=" + args["subtitles"])
        cmd.append(args["outfile"])
        return cmd

    def name(self):
        return "vlc"

    def start(self, args):
        # A stale socket from a previous run would stop the new one binding.
        self._remove_socket()
        super().start(args)

    def _ready(self):
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if os.path.exists(self.rc_socket_path):
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise ProcessException(
                    self._drain_error() or "vlc exited during startup"
                )
            time.sleep(0.1)
        raise ProcessException("vlc timed out starting up")

    def _drain_error(self):
        while True:
            line = self._readline(1)
            if not line:
                return None
            if any(marker in line for marker in _ERROR_MARKERS):
                return line

    # -- control ----------------------------------------------------------

    def _send(self, command):
        """
        Send one plaintext rc command and read what VLC says back.

        The terminator is the whole point. VLC's rc interface is a line-oriented
        interpreter: it reads a line, executes it and replies. Without the newline
        it waits for more input, so every control action was silently dropped and
        the UI showed a player that would not pause or seek.

        The reply is read so that "written" and "accepted" are distinguishable in
        the log. It is best-effort: a timeout means the command went out and VLC
        had nothing to say, which is not a failure, so it returns True rather than
        blocking the request thread that sent it.
        """
        if not os.path.exists(self.rc_socket_path):
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(self.rc_socket_path)
                sock.settimeout(_REPLY_TIMEOUT)
                sock.sendall((command + "\n").encode("utf-8"))
                reply = self._read_reply(sock)
        except OSError:
            return False
        cherrypy.log("VLC RC: " + command + " -> " + reply)
        return True

    @staticmethod
    def _read_reply(sock):
        """
        Read one line of VLC's response, or say why there wasn't one.

        Bounded so a player that never replies cannot hold up the thread serving
        the control request.
        """
        try:
            data = sock.recv(_REPLY_MAX_BYTES)
        except OSError:
            # A timeout. The command was written; VLC simply did not answer.
            return "no reply"
        if not data:
            return "no reply"
        text = data.decode("utf-8", "replace").strip()
        # VLC's rc opens with a version banner and closes with the status dump;
        # the first meaningful line is the useful part.
        for line in text.splitlines():
            line = line.strip()
            if line:
                return line[:_REPLY_MAX_CHARS]
        return "no reply"

    def _seek(self, seconds):
        return self._send("seek " + str(seconds))

    def control(self, action):
        """
        Map a BlissFlixx action onto an rc verb.

        Subtitle visibility is toggled rather than set, so show_subtitle and
        hide_subtitle share one command and the caller tracks which state it
        believes is current. That matches how omxplayer's key map behaves.
        """
        if action in ("pause", "resume"):
            self._send("pause" if action == "pause" else "play")
        elif action == "stop":
            self._send("quit")
        elif action == "plus30":
            self._seek(30)
        elif action == "minus30":
            self._seek(-30)
        elif action == "plus600":
            self._seek(600)
        elif action == "minus600":
            self._seek(-600)
        elif action == "volup":
            self._set_volume(self._current_volume() + self.opt("volume_step"))
        elif action == "voldown":
            self._set_volume(max(0, self._current_volume() - self.opt("volume_step")))
        elif action in (
            "next_subtitle",
            "prev_subtitle",
            "show_subtitle",
            "hide_subtitle",
        ):
            self._send("subtitle")
        # next_audio / prev_audio: rc has no track cycling command. Declared
        # absent in capabilities so the UI can hide the buttons.

    def _current_volume(self):
        return self._volume

    def _set_volume(self, value):
        value = min(value, self.opt("volume_max"))
        self._volume = value
        return self._send("vol " + str(value))

    def _remove_socket(self):
        if os.path.exists(self.rc_socket_path):
            try:
                os.remove(self.rc_socket_path)
            except OSError:
                pass

    def stop(self):
        self._remove_socket()
        super().stop()

"""
VLC backend.

VLC is the recommended player on current Raspberry Pi OS. Hardware H.264 decode,
HDMI audio and subtitles all work without the expensive GStreamer compositing
path, which makes it the most capable of the three non-omxplayer options.

Two things differ from the mpv backend:

- Control goes through VLC's cli interface, by typing plaintext commands into
  its stdin and reading the replies off its stdout. The rc interface would be
  the obvious choice, but this VLC has no rc module at all: Debian trixie's
  3.0.23 for armhf ships dummy, oldrc, dbus and others, and no
  librc_plugin.so, so --extraintf=rc is silently ignored and the socket never
  appears. The cli interface is a lua interface rather than a plugin, which is
  why it does not show up in `cvlc --list`, but it is present and it works.
  It is the same line-oriented protocol, so a command is only executed once its
  newline arrives.
- Readiness is the cli interface's own banner, "Command Line Interface
  initialized", rather than a socket appearing. That is a better signal than a
  path check: it is the control surface reporting that it is live.

Not implemented: audio track cycling and subtitle visibility. Neither has a cli
command equivalent, so they are declared absent rather than silently dropped.
"""

import os
import time
from queue import Empty, Queue

import cherrypy

from .backend import CAP_PAUSE, CAP_SEEK, CAP_STOP, CAP_VOLUME, PlayerBackend
from .processpipe import ProcessException

VLC_BIN = "cvlc"
_START_TIMEOUT = 30
#: Seconds to wait for VLC to acknowledge a command before giving up on a reply.
#: The command has already been written at that point, so this only bounds how
#: long the request thread can be held.
_REPLY_TIMEOUT = 2
#: Cap on the reply we read, so a chatty status dump cannot be pulled whole into
#: memory or the log.
_REPLY_MAX_BYTES = 4096
_REPLY_MAX_CHARS = 200

#: Printed by the cli interface once it is ready to accept commands. This is a
#: lua interface, not a plugin, so it does not show up in `cvlc --list`, but it
#: loads and it announces itself.
_CLI_READY_MARKER = "Command Line Interface initialized"
#: The cli interface prints this before each command it reads.
_CLI_PROMPT = ">"

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

    # No subtitle-visibility or audio-track command exists in the cli
    # interface, so those are declared absent rather than offered and dropped.
    capabilities = frozenset({CAP_PAUSE, CAP_STOP, CAP_SEEK, CAP_VOLUME})

    # The defaults are the invocation measured working on the Pi: DRM/KMS video
    # output through the vc4 driver, ALSA audio pinned to the HDMI card, and no
    # interactive interface since control arrives over the rc socket.
    defaults = {
        "binary": VLC_BIN,
        # cli, not dummy: the cli interface is what accepts the commands in
        # _send(). With dummy, and with stdin at EOF, it used to load anyway and
        # then shut down as soon as it started.
        "extra_args": ["--intf=cli"],
        "audio_device": "hdmi:CARD=vc4hdmi,DEV=0",
        "video_output": "drm_vout",
        "video_output_module": "vc4",
        # The Pi's screen is small, so VLC's default text size is too small to
        # read at typical viewing distance. 95 rather than 60 because of the
        # text rendering bug at this resolution.
        "subtitle_text_scale": "95",
        "start_timeout": _START_TIMEOUT,
        "volume_step": 5,
        "volume_max": 512,
    }

    def __init__(self, config=None):
        super().__init__(config=config)
        self.shell = False
        # Commands are typed into VLC's own stdin and its replies come back on
        # the same stdout it logs to, so it needs a pipe on stdin rather than
        # inheriting one. Without a pipe, and with stdin at EOF, the cli
        # interface shuts down the moment it starts.
        self.stdin_pipe = True
        self._ready_seen = False
        self._startup_errors = []
        self._replies = Queue()
        # Once the stage is running, the copier feeds every line the player
        # writes -- replies and log output alike -- through here. During startup
        # there is no copier yet and _ready() reads stdout itself.
        self.on_output_line = self._replies.put
        # The cli interface has no volume query, so it is tracked from the last
        # set point. VLC treats 256 of 512 as its default level.
        self._volume = 256

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

    def _ready(self):
        """
        Wait for the cli interface to announce itself.

        This used to wait for the rc unix socket, which cannot exist on this
        machine: Debian trixie's VLC 3.0.23 for armhf ships no librc_plugin.so,
        so --extraintf=rc was silently ignored and the socket was never created.
        _ready() therefore always ran out its full timeout and failed.

        The cli interface is a lua interface rather than a plugin, so it does not
        appear in `cvlc --list`, but it is present and it prints a banner once it
        is ready for commands. That banner is the readiness signal, and it is a
        better one than a path check: it is the control surface saying it is
        live, rather than a file being assumed to mean so.

        Lines are also queued on the way past, so the control path has the
        startup chatter available when the first command arrives.
        """
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                raise ProcessException(
                    self._drain_error() or "vlc exited during startup"
                )
            try:
                line = self._readline(0.5)
            except ProcessException:
                continue
            self._replies.put(line)
            if _CLI_READY_MARKER in line:
                self._ready_seen = True
                return
            if any(marker in line for marker in _ERROR_MARKERS):
                # Defer this one. The interface reads its next command from
                # stdin, so it does not stop talking when a line is rejected --
                # it carries on and keeps going, and every later line looks the
                # same. Treating the first bad line as fatal meant a message
                # about, say, a missing audio filter ended the stage while the
                # player was in fact still running and playing.
                if line not in self._startup_errors:
                    self._startup_errors.append(line)
        raise ProcessException(self._did_not_come_up())

    def _did_not_come_up(self):
        """
        Say why the interface never appeared, using whatever it complained about
        on the way. A bare "did not come up" is the one message that helps
        nobody, and these lines are the only evidence there is.
        """
        detail = " | ".join(self._startup_errors[:3])
        if detail:
            return "vlc cli interface did not come up: " + detail
        return "vlc cli interface did not come up"

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
        Type one command into VLC's stdin and read what it says back.

        The newline is the whole point, and it is the same bug the rc socket had.
        VLC's cli interface is a line interpreter: it reads until it sees a
        newline, executes what it has, and replies. Without it the command is
        never executed and never rejected -- it just sits there -- which is
        exactly how every control action came to be dropped silently.

        The reply is read so "written" and "accepted" are distinguishable in the
        log. Best-effort by design: a timeout means the command went out and VLC
        had nothing to say, which is not a failure, so this returns True rather
        than blocking the request thread that sent it.
        """
        proc = getattr(self, "proc", None)
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write((command + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            return False
        reply = self._await_reply(command)
        cherrypy.log("VLC CLI: " + command + " -> " + reply)
        return True

    def _await_reply(self, command):
        """
        Wait for the cli interface to echo the command back and answer it.

        The interface echoes what it was given, so the echo is used as a marker:
        everything after it up to the next prompt is that command's answer. That
        is what makes replies attributable, since a single stream also carries
        every log line the player writes while it plays.
        """
        deadline = time.time() + _REPLY_TIMEOUT
        echoed = False
        collected = []
        while time.time() < deadline:
            try:
                line = self._replies.get(timeout=0.2)
            except Empty:
                continue
            if not echoed:
                # The echo arrives as "> command" or just the command.
                if command in line:
                    echoed = True
                continue
            line = line.strip()
            if not line or line == _CLI_PROMPT:
                break
            collected.append(line)
        if not echoed:
            return "no reply"
        if not collected:
            return "ok"
        return " / ".join(collected)[:_REPLY_MAX_CHARS]

    def _seek(self, seconds):
        return self._send("seek " + str(seconds))

    def control(self, action):
        """
        Map a BlissFlixx action onto a cli verb.

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
        # subtitle visibility, next_audio, prev_audio: the cli interface has no
        # command for any of them. Declared absent in capabilities so the UI can
        # hide the buttons rather than offer one that does nothing.

    def _current_volume(self):
        return self._volume

    def _set_volume(self, value):
        value = min(value, self.opt("volume_max"))
        self._volume = value
        # "volume N", not "vol N": the verb is "volume", and there is no "vol".
        # The cli interface reports an unrecognised command by echoing the
        # prompt and saying nothing at all, which is indistinguishable from
        # success unless you know the verb list.
        return self._send("volume " + str(value))

    def stop(self):
        super().stop()

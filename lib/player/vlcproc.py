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
import re
import time
from queue import Empty, Queue

import cherrypy

from .backend import (
    CAP_AUDIO_TRACK,
    CAP_PAUSE,
    CAP_SEEK,
    CAP_STOP,
    CAP_SUBTITLES,
    CAP_VOLUME,
    PlayerBackend,
)
from .processpipe import ProcessException, _start_thread

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
#: VLC prefixes every line of its own logging with a bracketed thread id, e.g.
#: "[007070c8] main audio output error: ...". Those lines share stdout with the
#: cli interface's replies and are not answers to anything.
_VLC_LOG_PREFIX = re.compile(r"^\[[0-9a-fA-F]+\]")
#: How long to keep listening after a reply arrives, for a trailing line.
_REPLY_SETTLE = 0.3


def _is_player_logging(line):
    return bool(_VLC_LOG_PREFIX.match(line.strip()))


#: A track id as printed in a strack/atrack listing, e.g. "2 - English (CC)".
#: VLC pipes its listings with a leading "| " on each line, so that is stripped
#: before matching. This pattern is why track control appeared to do nothing:
#: every listing arrived, and none of it matched.
_TRACK_ID = re.compile(r"^\|?\s*(-?\d+)\s+-")
#: How long to keep asking for a subtitle track once the player is up. The
#: listing is empty until VLC has opened the media, and a torrent being streamed
#: over http can take a while to start producing.
_SUBTITLE_WAIT_TIMEOUT = 60
#: "( time: 1834.221 )" and "( length: 7182.429 )" -- what get_time and
#: get_length answer with. Matched strictly, including the label, so a number
#: inside the player's own logging cannot be read as an answer.
_NUMBER_IN_PARENS = re.compile(r"\(\s*(?:time|length)\s*:\s*([0-9.]+)\s*\)")


def _format_seconds(value):
    """
    Seconds as h:mm:ss, which is how a person reads a position.
    """
    if value is None:
        return "--:--"
    total = int(value)
    return "%d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


#: VLC's id for "no subtitles", as printed in the listing.
_TRACK_DISABLED = -1


def _summarise_reply(lines):
    """
    Render collected reply lines for the log.

    A track listing is many lines, so it is folded to its track ids rather than
    quoted in full; anything else is joined as it arrived.
    """
    if not lines:
        return "no reply"
    ids = []
    for line in lines:
        found = _TRACK_ID.match(line.strip())
        if found:
            ids.append(found.group(1))
    if ids and len(ids) == len([x for x in lines if x.strip()]):
        return "tracks " + ", ".join(ids)
    return " / ".join(lines)[:_REPLY_MAX_CHARS]


def _is_echo(line):
    """
    True for the interface echoing back what it was given.

    The prompt is not a reliable marker for where an answer starts, because the
    copier drains the stream faster than a command is sent. But the echo of the
    command itself is still noise in the reply, and stripping it keeps the log
    about the answer rather than about what was asked.
    """
    text = line.strip()
    if not text.startswith(">"):
        return False
    return len(text) > 1


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

    capabilities = frozenset(
        {
            CAP_AUDIO_TRACK,
            CAP_PAUSE,
            CAP_STOP,
            CAP_SEEK,
            CAP_VOLUME,
            CAP_SUBTITLES,
            CAP_AUDIO_TRACK,
        }
    )

    # The defaults are the invocation measured working on the Pi: DRM/KMS video
    # output through the vc4 driver, ALSA audio pinned to the HDMI card, and no
    # interactive interface since control arrives over the rc socket.
    defaults = {
        "binary": VLC_BIN,
        # cli, not dummy: the cli interface is what accepts the commands in
        # _send_command(). With dummy, and with stdin at EOF, it used to load anyway and
        # then shut down as soon as it started.
        "extra_args": ["--intf=cli"],
        "audio_device": "hdmi:CARD=vc4hdmi,DEV=0",
        "video_output": "drm_vout",
        "video_output_module": "vc4",
        # The Pi's screen is small, so VLC's default text size is too small to
        # read at typical viewing distance. 95 rather than 60 because of the
        # text rendering bug at this resolution.
        "subtitle_text_scale": "95",
        # Off by default: get_time and get_length are two extra commands per
        # report, and most people are not watching the log while a film plays.
        "report_progress": "0",
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
        # Last track id chosen per interface verb, so stepping does not have to
        # re-read the listing every time. See _step_track.
        self._track_choice = {}
        self._track_listing = {}
        # Once the stage is running, the copier feeds every line the player
        # writes -- replies and log output alike -- through here. During startup
        # there is no copier yet and _ready() reads stdout itself.
        #
        # _ready() swaps this for a fresh queue once it has what it was waiting
        # for, so on_output_line has to be rebound with it rather than holding
        # the old queue's put.
        self._reset_replies()

    def _reset_replies(self):
        self._replies = Queue()
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
            # OSD is what tells the user their action landed. Without it a
            # pause or a seek is invisible on the screen, so the only feedback
            # is whatever the log says, which nobody watching a film can see.
            "--osd",
            # The startup title overlay is not wanted: it covers the picture
            # while the film begins and is not what --osd is for.
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

        The lines read on the way are discarded once readiness is established.
        They were queued for the first command to have context, but the interface
        does not speak until spoken to, so a ten second old banner came back as
        the answer to whatever was sent first.
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
                # Drop everything read while starting. The banner and the
                # version line were queued so the first command would have
                # context, but the interface is quiet until it is spoken to, so
                # they just sat there: the first control action came back
                # reporting the startup banner as if it were its own answer.
                self._reset_replies()
                self._enable_embedded_subtitles()
                self._start_progress_reporter()
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

    def _send_command(self, command):
        """
        Type one command into VLC's stdin and read what it says back.

        Named _send_command rather than _send on purpose. Process._send is the
        pipe protocol's own method, and this overrode it by accident: when the
        stage reported readiness, msg_ready() called the inherited _send with
        (msg, args) and raised

            TypeError: VlcProcess._send() takes 2 positional arguments but 3
            were given

        which killed the stage thread at exactly the moment it was trying to
        hand control to the next stage. Nothing in the unit tests noticed,
        because they call this directly and never start a stage through
        ExternalProcess.start().

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
        lines = self._send_and_collect(command)
        if lines is None:
            return False
        cherrypy.log("VLC CLI: " + command + " -> " + _summarise_reply(lines))
        return True

    def _send_and_collect(self, command):
        """
        Write one command and return the lines that came back, or None.

        Sending and reading are one operation rather than two on purpose. A
        track listing is the reply to the command, so asking for it and then
        reading the queue separately means the read drains the very lines that
        were wanted and finds nothing: "strack" arrived, was consumed as that
        command's reply, and the listing was gone. Both callers need the lines
        rather than a string, so this is the shared part.
        """
        proc = getattr(self, "proc", None)
        if proc is None or proc.poll() is not None:
            return None
        try:
            proc.stdin.write((command + "\n").encode("utf-8"))
            proc.stdin.flush()
        except (OSError, ValueError):
            return None
        return self._collect_reply()

    def _collect_reply(self, settle=_REPLY_SETTLE):
        """
        Take the lines that arrive shortly after a command, ignoring noise.

        There is no marker for where a command's answer begins, and this does not
        pretend otherwise. The interface echoes the command back, but it writes
        its prompt and replies to the same stdout it logs to, and the output
        copier drains that continuously, so the echo has usually been consumed by
        the time anything is sent. Attributing replies by the echo gave "no
        reply" for every command while the commands were working perfectly.

        So this keeps whatever arrives in a short window and drops the player's
        own logging, which is recognisable by the bracketed thread id VLC puts
        on every line. What is left is the answer: "( state paused )",
        "( audio volume: 300 )", or a track listing.

        A command the interface ignored looks exactly like one it had nothing to
        say about. That is a real limitation of this interface, not a bug, and it
        is why the log always names the command alongside whatever came back.
        """
        deadline = time.time() + _REPLY_TIMEOUT
        collected = []
        while time.time() < deadline:
            try:
                line = self._replies.get(timeout=0.1)
            except Empty:
                # A short silence ends the wait rather than spending the whole
                # budget on a player with nothing to say.
                if collected or time.time() > deadline - settle:
                    break
                continue
            if _is_player_logging(line) or _is_echo(line):
                continue
            collected.append(line)
        return collected

    def _report_progress(self):
        """
        Log where the film is and how long it is.

        VLC's cli answers get_time and get_length with a line each:

            ( time: 1834.221 )
            ( length: 7182.429 )

        which is the only way to see either on this build. There is no verb for
        drawing text on screen -- marq is absent -- so this goes to the log and
        not to the picture. VLC's own --osd still covers volume and seek targets;
        it just cannot be told what to say.

        Best-effort and bounded like every other command here: a player that
        answers nothing must not hold up the pipe that asked.
        """
        position = self._read_number("get_time")
        length = self._read_number("get_length")
        if position is None and length is None:
            return None
        cherrypy.log(
            "VLC progress: position %s, length %s"
            % (_format_seconds(position), _format_seconds(length))
        )
        return position, length

    def _read_number(self, verb):
        """
        Send a query and pull the number out of its reply.

        The reply is attributed the same way a control command's is: take the
        lines that arrive and pick the one that parses, ignoring the player's
        own logging. "no reply" comes back as None rather than raising, because
        a player that has not opened its media yet has no time to report and
        that is not a failure.
        """
        for line in self._send_and_collect(verb) or []:
            found = _NUMBER_IN_PARENS.search(line.strip())
            if found:
                try:
                    return float(found.group(1))
                except ValueError:
                    return None
        return None

    def _seek(self, seconds):
        """
        Seek forwards or backwards from wherever the film is now.

        The sign is not decoration and omitting it is a real bug. VLC's cli
        routes seek through common.seek in share/lua/modules/common.lua:

            if string.sub(value,1,1) == "+" or string.sub(value,1,1) == "-" then
                vlc.var.set(input,"time",vlc.var.get(input,"time") + pos)
            else
                vlc.var.set(input,"time",pos)
            end

        Without a leading sign that is an absolute seek, so "plus30" jumped to
        00:30 of the film instead of moving 30 seconds on from wherever you were.
        Every other backend takes a plain signed number, which is why only VLC
        misbehaved and why it looked like a mapping error rather than a syntax
        one.
        """
        return self._send_command("seek %+d" % seconds)

    def control(self, action):
        """
        Map a BlissFlixx action onto a cli verb.

        Subtitle visibility is toggled rather than set, so show_subtitle and
        hide_subtitle share one command and the caller tracks which state it
        believes is current. That matches how omxplayer's key map behaves.
        """
        if action in ("pause", "resume"):
            self._send_command("pause" if action == "pause" else "play")
        elif action == "stop":
            self._send_command("quit")
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
        elif action == "hide_subtitle":
            # -1 is VLC's "disabled", and it is always in the listing.
            self._send_command("strack " + str(_TRACK_DISABLED))
        elif action == "show_subtitle":
            self._show_track("strack")
        elif action == "next_subtitle":
            self._step_track("strack", +1)
        elif action == "prev_subtitle":
            self._step_track("strack", -1)
        elif action == "next_audio":
            self._step_track("atrack", +1)
        elif action == "prev_audio":
            self._step_track("atrack", -1)

    def _start_progress_reporter(self):
        """
        Report position and length periodically, if configured to.

        Off unless report_progress is set to a positive number of seconds, which
        is the interval. Nothing is spawned otherwise.
        """
        try:
            interval = int(self.opt("report_progress"))
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            return
        _start_thread(self._progress_loop, interval)

    def _progress_loop(self, interval):
        while not self.killing:
            time.sleep(interval)
            if self.killing:
                return
            self._report_progress()

    def _enable_embedded_subtitles(self):
        """
        Turn on a subtitle track when the file has one.

        VLC starts with none selected -- sub-track=-1 -- so a file with embedded
        subtitles played silently unless somebody pressed the subtitle button
        first. omxplayer never had that problem because OmxplayerProcess2.start()
        has always called show_subtitle; this brings VLC to the same behaviour,
        which is what "subtitles are mandatory" means in practice.

        Only for tracks inside the file. When the UI asked for subtitles by
        language, they arrive as --sub-file and VLC selects them itself, and
        overriding that would be second-guessing an explicit choice.

        Done on a background thread, and retried, because none of it can happen
        yet. Asking for the track listing at readiness returns nothing at all:
        VLC has printed its banner but has not opened the media, so there are no
        tracks to list, and "no subtitle track in this file" is the truth at that
        moment rather than a fact about the file. Waiting here instead would
        delay readiness -- and with it the whole pipeline -- by however long the
        media takes to open, for a subtitle track that may not even exist.

        So readiness returns immediately and this waits in the background for the
        tracks to appear, the way a player would once it knows what it is
        playing. If none turn up within the window, nothing is sent.
        """
        # args is set by _get_cmd on the normal start path. Absent when _ready
        # is exercised on its own, which is treated as "no external subs".
        args = getattr(self, "args", None) or {}
        if "subtitles" in args:
            return
        _start_thread(self._wait_for_subtitles, args)

    def _wait_for_subtitles(self, args):
        deadline = time.time() + _SUBTITLE_WAIT_TIMEOUT
        while time.time() < deadline:
            if self.killing:
                return
            proc = getattr(self, "proc", None)
            if proc is None or proc.poll() is not None:
                return
            tracks = [t for t in self._track_ids("strack") if t != _TRACK_DISABLED]
            if tracks:
                cherrypy.log(
                    "enabling subtitle track %s "
                    "(omxplayer does this on every start)" % tracks[0]
                )
                self._track_choice["strack"] = tracks[0]
                self._send_command("strack " + str(tracks[0]))
                return
            # The listing is empty because the media is not open yet, not because
            # there is nothing there. Give it a moment before asking again.
            time.sleep(1.0)
        cherrypy.log("no subtitle track appeared in this file")

    def _show_track(self, command):
        """
        Turn a track back on.

        There is no "enable" command, so a real track id has to be sent. The
        last one chosen is preferred, so show/hide/show returns to the same
        track rather than jumping somewhere else; otherwise the first track in
        the listing.

        This used to send "strack 0" on the assumption that 0 meant "the first
        one". It does not. A real listing on the Pi came back as

            | -1 - Disable
            | 2 - English (CC) - [English]

        so 0 is not a track at all, and asking for it turned subtitles back on
        for nobody.
        """
        target = self._track_choice.get(command)
        if target is None or target == _TRACK_DISABLED:
            tracks = self._track_listing.get(command) or self._track_ids(command)
            real = [t for t in tracks if t != _TRACK_DISABLED]
            if not real:
                return False
            target = real[0]
            self._track_choice[command] = target
        return self._send_command(command + " " + str(target))

    def _step_track(self, command, direction):
        """
        Move to the next or previous subtitle or audio track.

        "strack" and "atrack" with no argument list the available tracks and
        mark the active one with an asterisk:

            +----[ spu-es ]
            | -1 - Disable
            | 2 - English (CC) - [English] *
            +----[ end of spu-es ]

        There is no "next track" verb, so the current position is read from that
        listing and an adjacent id is chosen. Stepping past either end wraps: the
        listing always includes -1, which disables the track, so a step forward
        from the last track goes to -1 and a step back from -1 goes to the last.

        The active id is cached after the first listing, because the listing is
        the only way to find it and it costs a round trip.
        """
        tracks = self._track_ids(command)
        if not tracks:
            return False
        current = self._track_choice.get(command)
        if current is None or current not in tracks:
            current = next((t for t in tracks if t != _TRACK_DISABLED), None)
            if current is None:
                return False
        index = tracks.index(current)
        target = tracks[(index + direction) % len(tracks)]
        self._track_choice[command] = target
        return self._send_command(command + " " + str(target))

    def _track_ids(self, command):
        """
        Ask for the track listing and pull the ids out of it.

        Replies come back on the shared stdout, so this reads the same queue
        _send_command does and filters out the player's own logging.
        """
        lines = self._send_and_collect(command)
        if lines is None:
            return []
        ids = []
        for line in lines:
            found = _TRACK_ID.match(line.strip())
            if found:
                ids.append(int(found.group(1)))
        if ids:
            self._track_listing[command] = ids
        # Log the listing whether or not anything matched. A command that was
        # sent but matched nothing looks exactly like one that was never sent --
        # both log nothing at all -- and that is what made the unanchored
        # pattern hard to find: the output was plainly arriving and simply
        # being ignored.
        cherrypy.log("VLC CLI: " + command + " -> " + _summarise_reply(lines))
        return self._track_listing.get(command, [])

    def _current_volume(self):
        return self._volume

    def _set_volume(self, value):
        value = min(value, self.opt("volume_max"))
        self._volume = value
        # "volume N", not "vol N": the verb is "volume", and there is no "vol".
        # The cli interface reports an unrecognised command by echoing the
        # prompt and saying nothing at all, which is indistinguishable from
        # success unless you know the verb list.
        return self._send_command("volume " + str(value))

    def stop(self):
        super().stop()

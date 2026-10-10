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
import threading
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
from .processpipe import TMP_DIR, ProcessException, _start_thread

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


#: Noise the interface prints in place of an answer, on its own, with none of
#: the bracketed thread id that marks the player's own logging. It arrives as
#: the entire reply to get_length, so the number never reaches the caller and
#: every duration reads as unknown:
#:
#:     error: XDG_RUNTIME_DIR is invalid or not set in the environment.
#:
#: Harmless -- it is about the environment the interface is running in, not the
#: film -- but it occupies the reply window, so it has to be recognised as what
#: it is rather than taken for the answer.
_NOISE_REPLY = re.compile(r"^(error:\s*XDG_RUNTIME_DIR|\s*$)", re.IGNORECASE)


def _is_noise(line):
    return bool(_NOISE_REPLY.match(line.strip()))


#: A track id as printed in a strack/atrack listing, e.g. "2 - English (CC)".
#: VLC pipes its listings with a leading "| " on each line, so that is stripped
#: before matching. This pattern is why track control appeared to do nothing:
#: every listing arrived, and none of it matched.
#: The language VLC reports for a track, taken from the listing line:
#:
#:     | 2 - English (CC) - [English] *
#:
#: The bracketed form is the track's own language and is preferred. The plain
#: form is the fallback for tracks whose language VLC does not know, where the
#: bracketed part repeats what is already in front of it.
_TRACK_NAME = re.compile(r"^\|?\s*(-?\d+)\s+-\s*(.*?)\s*(?:-|$)")


def _track_language(line):
    """
    Pull the language out of one line of a track listing.

        | 2 - English (CC) - [English] *

    The bracketed part is the track's own language and is preferred, since the
    plain part can be anything -- a forced-narration marker, a title. Where the
    two are the same, which is what happens when VLC knows nothing more than the
    language, the brackets are empty of new information and the plain part is
    used, so a duplicate is not put on the screen.

    Returns None when there is nothing to show, which is what makes the caller
    fall back to the plain word rather than to an empty "[on] ".
    """
    found = _TRACK_NAME.match(line.strip())
    if not found:
        return None
    rest = found.group(2).strip()
    bracketed = re.search(r"\[([^\]]+)\]\s*\*?\s*$", rest)
    plain = re.sub(r"\s*\[[^\]]*\]\s*\*?\s*$", "", rest).strip()
    if bracketed:
        language = bracketed.group(1).strip()
        if language and language != plain:
            return language
    return plain or None


_TRACK_ID = re.compile(r"^\|?\s*(-?\d+)\s+-")
#: How long to keep asking for a subtitle track once the player is up. The
#: listing is empty until VLC has opened the media, and a torrent being streamed
#: over http can take a while to start producing.
_SUBTITLE_WAIT_TIMEOUT = 60
#: Where the marquee reads its text from. VLC re-reads it every --marq-refresh
#: seconds, so writing to it is how the position gets on the screen.
MARQ_FILE = os.path.join(TMP_DIR, "marq.txt")
#: What get_time and get_length answer with: a number on a line of its own,
#: e.g. "1297". Not "( length: 1297 )" -- that was assumed, tested against the
#: real player and wrong, which is why nothing was ever reported.
#:
#: The leading ">" is the interface's prompt, which it writes to the same line
#: as the answer about as often as not:
#:
#:     > 1297
#:
#: So the same reply arrives sometimes bare and sometimes with the prompt on
#: the front of it, and matching only the bare form is why a seek's time
#: appeared "sometimes" and not otherwise -- it was whichever form the
#: interface happened to emit for that one reply.
#:
#: Anchored to the whole line so a number appearing inside the player's own
#: logging cannot be read as an answer. Those lines are already dropped as
#: logging before this is applied; this is the second line of defence.
_NUMBER_ALONE = re.compile(r"^(?:\s*>\s*)*\(?\s*(\d+(?:\.\d+)?)\s*\)?\s*$")


def _format_seconds(value):
    """
    Seconds as h:mm:ss, which is how a person reads a position.
    """
    if value is None:
        return "--:--"
    total = int(value)
    return "%d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def _format_clock(value):
    """
    Shorter form for the screen: m:ss under an hour, h:mm:ss over it.

    A position is glanced at, not read, so the leading zero hour is dropped and
    the hours are left off entirely when there are none. An unknown length shows
    as "--" rather than "--:--", which is needlessly long for a glance.
    """
    if value is None:
        return "--"
    total = int(value)
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, seconds)
    return "%d:%02d" % (minutes, seconds)


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


#: How far each seek action moves, in seconds.
_SEEK_SECONDS = {
    "plus30": 30,
    "minus30": -30,
    "plus600": 600,
    "minus600": -600,
}


#: VLC's own volume scale. Its maximum is 512, not 100, and its default is
#: 256 -- which is what the player starts at and what the cli has no command to
#: read back, so the level is tracked from the last one set.
_VOLUME_MAX = 512


def _volume_percent(level):
    """
    VLC's level as a percentage.

    Shown this way because 512 is VLC's internal scale and means nothing to
    anyone watching. Its default of 256 is 50%, which is also what the player
    starts at -- so "Volume 50%" is the first thing seen, rather than the 256
    that used to appear.
    """
    return round(level * 100 / _VOLUME_MAX)


def _track_label(name, chosen, language=None):
    """
    What the overlay says after a track change.

    "[on] English" rather than "Subtitle English", because that is what it is:
    the state, then the language. With no language to name -- a track VLC does
    not describe, or one not yet chosen -- it falls back to the plain word, and
    never to a number, since "subtitle 0" would be claiming something not known.

    -1 is how VLC spells "none", so it reads as "off" rather than as a track
    numbered minus one.
    """
    if chosen is None:
        return name
    if chosen < 0:
        return "[off] %s" % name
    if language:
        return "[on] %s" % language
    return "[on] %s" % name


#: What the cli interface prints when it is ready for the next command. The only
#: reliable end-of-exchange marker: the reply cannot be attributed by its echo,
#: and the output never goes quiet on a playing film.
_CLI_PROMPT = ">"


def _is_prompt(line):
    """
    True for the ">" the interface prints when it is ready for the next command.

    Worth knowing about because it is the only reliable end-of-exchange signal
    there is. The reply cannot be attributed by its echo -- the copier drains
    that away -- and the output never goes quiet on a playing film, so neither
    silence nor an echo marks the end. The prompt does.
    """
    return line.strip() == _CLI_PROMPT


def _is_echo(line):
    """
    True for the interface echoing back what it was given.

    The prompt is not a reliable marker for where an answer starts, because the
    copier drains the stream faster than a command is sent. But the echo of the
    command itself is still noise in the reply, and stripping it keeps the log
    about the answer rather than about what was asked.

    Only the prompt followed by the command. The interface writes its prompt to
    the front of the reply as often as it writes it on a line of its own -- "> 9"
    rather than "9" -- and this took any line starting with ">" as an echo, so
    every answer written that way was dropped before it could be read. That is
    why a seek showed its time only sometimes: it was whichever form the
    interface happened to use for that one reply.
    """
    text = line.strip()
    if not text.startswith(">"):
        return False
    return len(text) > 1 and not _NUMBER_ALONE.match(text)


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
        # Also log position and duration, not just show them on screen.
        "report_progress": "0",
        # The on-screen position overlay. marq is a "sub source" in VLC 3 --
        # not a video filter, so --video-filter fails to load it -- and it is
        # the only component here that can put text on the picture.
        "osd_overlay": "1",
        # marq re-reads the file on this interval, so a new message appears
        # within about a second of the action that caused it.
        "osd_refresh": "1",
        # How long a message stays on screen, in milliseconds. Zero would leave
        # it there for ever, which would be worse than not showing it at all.
        "osd_timeout": "3000",
        # How far the subtitles sit above the bottom edge, in destination pixels.
        # VLC applies this to the subtitle region alone (vout_subpictures.c lifts
        # the region by y_margin), so it is the only thing that moves them.
        #
        # Small on purpose. At 220 they sat about 40% up the picture, which is
        # nowhere near the bottom where subtitles belong -- it had been raised to
        # clear the overlay, which no longer needs it now the overlay is at the
        # top. Just enough to keep the descenders off the edge of the frame.
        "sub_margin": "24",
        # An alignment enum, not a 1-10 scale: marq passes this straight
        # through as the subpicture region's i_align. The values marq offers are
        # 0 center, 1 left, 2 right, 4 top, 8 bottom. Guessing at a scale put
        # the message on the right of the screen; 4 is the top edge, which is
        # what a confirmation belongs at and well clear of the subtitles.
        "osd_position": "4",
        # Big enough to read from a sofa. VLC measures this in pixels of the
        # source frame, so it scales with resolution: at 1080p 28px was small.
        "osd_size": "84",
        "osd_opacity": "220",
        "start_timeout": _START_TIMEOUT,
        # As a percentage of VLC's 0-512 scale, not a number of its units: the
        # step used to be 5, which is 5 of 512 -- about one percent, so the
        # button appeared to do nothing at all. It is converted on the way out.
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
        # Track id to language, from the listing the ids came from; the listing
        # is the only place a track's language appears.
        self._track_names = {}
        # Set the moment the user asks for anything; the subtitle search gives
        # up when it is set.
        #
        # They and the user want the same thing: one command at a time on one
        # stdin, with the reply matched to the command that was sent. A search
        # polling in the background was swallowing the reply to somebody's ffwd,
        # so the confirmation said nothing. It is only ever more useful than the
        # user while they are still waiting for playback to start.
        self._acted = False
        # Once the stage is running, the copier feeds every line the player
        # writes -- replies and log output alike -- through here. During startup
        # there is no copier yet and _ready() reads stdout itself.
        #
        # _ready() swaps this for a fresh queue once it has what it was waiting
        # for, so on_output_line has to be rebound with it rather than holding
        # the old queue's put.
        self._reset_replies()
        self._create_overlay_file()

    def _create_overlay_file(self):
        """
        Put the marquee file there before VLC starts reading it.

        marq logs "cannot open ...: No such file or directory" once per refresh
        tick, and refresh is every second, so from startup until the first action
        the whole film is accompanied by that. It is made here rather than left
        for the first control() to write.

        Skipped when the overlay is off, since VLC is not started with --marq-file
        then and nothing reads it.

        It is created holding a space, not empty: getline() returns -1 at end of
        file and marq reports that as "Invalid argument", so a zero-byte file
        fails exactly like a missing one.
        """
        if not self._overlay_wanted():
            return
        try:
            os.makedirs(os.path.dirname(MARQ_FILE), exist_ok=True)
            if not os.path.exists(MARQ_FILE):
                with open(MARQ_FILE, "w", encoding="utf-8") as handle:
                    handle.write(" ")
        except OSError as exc:
            # Not fatal: _write_overlay creates it on the first action and warns
            # if it cannot. This only saves the ticks before then.
            cherrypy.log("could not create the VLC overlay file: %s" % exc)

    def _reset_replies(self):
        self._replies = Queue()
        self.on_output_line = self._replies.put
        # The cli interface has no volume query, so it is tracked from the last
        # set point. VLC treats 256 of 512 as its default level.
        self._volume = 256
        self._overlay_shown = None
        self._overlay_failures = 0
        # Remembered once learned; see _report_progress.
        self._length = None
        # One command at a time. Two threads ask this player things at once --
        # the overlay reporter polls get_time every second, and the subtitle
        # waiter polls strack for up to a minute -- and they share one stdin and
        # one reply queue. So a strack reply could be taken as the answer to
        # get_length, which is why the duration never appeared, and their
        # commands could interleave mid-line on stdin.
        #
        # Serialised rather than given separate reply paths, because separate
        # paths would mean separate readers on one pipe -- the buffered-reader
        # bug all over again.
        self._command_lock = threading.Lock()

    @property
    def start_timeout(self):
        return self.opt("start_timeout")

    def build_command(self, args):
        cmd = [self.opt("binary")]
        cmd += list(self.opt("extra_args"))
        cmd += self._osd_args()
        cmd += [
            "--aout=alsa",
            "--alsa-audio-device=" + self.opt("audio_device"),
            "--vout=" + self.opt("video_output"),
            "--drm-vout-module=" + self.opt("video_output_module"),
            "--sub-text-scale=" + self.opt("subtitle_text_scale"),
            "--sub-margin=" + self.opt("sub_margin"),
            # A run that must not stop on its own.
            # OSD is what tells the user their action landed. Without it a
            # pause or a seek is invisible on the screen, so the only feedback
            # is whatever the log says, which nobody watching a film can see.
            "--osd",
            # VLC's own title, for the first few seconds of the film.
            #
            # This was --no-video-title-show, and the title was written by us
            # instead: a background thread polling get_length until the player
            # would answer. That was the one thing that stopped the cli
            # interface answering anything -- it went quiet within twenty seconds
            # of playback, taking the reply to every seek with it, which is why a
            # seek's confirmation so often had no time on it.
            #
            # VLC already does this, knows the duration without being asked, and
            # does not need to be spoken to. Asking it in its own words costs
            # nothing and cannot silence it.
            "--video-title-show",
            "--video-title-timeout=" + str(self.opt("osd_timeout")),
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
        with self._command_lock:
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
        last_line_at = time.time()
        while time.time() < deadline:
            try:
                line = self._replies.get(timeout=0.1)
            except Empty:
                # A pause in the output ends the wait. It used to end as soon as
                # anything had been collected, which was wrong: this player
                # interleaves messages that do not carry the bracketed thread id
                # -- "Device or resource busy" and the like -- so those were
                # being taken for answers, and the wait then stopped before the
                # real reply arrived. That is why the position read 0:00 while
                # get_time was answering perfectly well a moment later.
                if time.time() - last_line_at >= settle:
                    break
                continue
            last_line_at = time.time()
            if _is_player_logging(line) or _is_echo(line) or _is_noise(line):
                continue
            collected.append(line)
        return collected

    def _osd_args(self):
        """
        Arguments for the on-screen position overlay.

        marq is a "sub source" in VLC 3, not a video filter -- passing it to
        --video-filter fails with "Failed to create video filter 'marq'" -- so
        it is added as a sub source, which is what lets it draw over the picture
        alongside real subtitles rather than replacing them.

        The text is not given on the command line because VLC has no cli
        command to change it. It is given as a file, and marq re-reads that file
        every --marq-refresh seconds, so the reporter writing to it is what keeps
        the position live. Verified on the Pi: modules/spu/marq.c calls
        MarqueeReadFile() from its Filter() on every refresh.

        Returned empty when osd_overlay is off, so the arguments are not merely
        ignored -- marq is never asked for.
        """
        if str(self.opt("osd_overlay")).lower() in ("0", "false", "", "none"):
            return []
        return [
            "--sub-source=marq",
            "--marq-file=" + MARQ_FILE,
            "--marq-refresh=" + str(self.opt("osd_refresh")),
            "--marq-timeout=" + str(self.opt("osd_timeout")),
            "--marq-position=" + str(self.opt("osd_position")),
            "--marq-size=" + str(self.opt("osd_size")),
            "--marq-opacity=" + str(self.opt("osd_opacity")),
        ]

    def _where(self, label):
        """
        The confirmation text with the position and duration on it.

        "Paused 1:43 / 21:37" rather than "Paused". Falls back to the bare label
        when the player will not say, so a confirmation is never missing
        altogether -- which is the case this replaced, where a refused read left
        the previous message on screen and looked like the action had done
        nothing.
        """
        # _read_position rather than _report_progress: that one writes to the
        # overlay itself, and this is the text _where is going to hand it, so
        # using it would write twice and the second write would win with no
        # label on it.
        reported = self._read_position()
        if not reported:
            return label
        position, length = reported
        return "%s %s / %s" % (
            label,
            _format_clock(position),
            _format_clock(length),
        )

    def _read_position(self):
        """
        Where we are and how long the film is, or None if the player will not say.

        Split out of _report_progress so that a caller supplying its own text --
        "Paused 1:43 / 21:37" -- can use the numbers without also being written
        over by the plain ones.
        """
        position = self._read_number("get_time")
        # A film's length does not change while it plays, so once it has answered
        # it is not asked again. Until then it is asked on every read, because
        # VLC cannot report a length for the first moments of a stream: caching
        # that first failure showed "--" on the confirmations until some later
        # seek happened to work and correct it by accident.
        if self._length is None:
            length = self._read_number("get_length")
            if length is not None:
                self._length = length
        if position is None:
            return None
        return position, self._length

    def _report_progress(self):
        """
        Ask where we are and show it.

        One shot, called after a seek rather than on a timer. Reading the clock
        continuously is what stopped this player answering anything at all after
        a few seconds of playback; reading it once, when the user has just asked
        to move, is answered reliably.
        """
        reported = self._read_position()
        if reported is None:
            return None
        position, length = reported
        self._show_overlay("%s / %s" % (_format_clock(position), _format_clock(length)))
        if str(self.opt("report_progress")).lower() not in ("0", "", "false"):
            cherrypy.log(
                "VLC progress: position %s, length %s"
                % (_format_seconds(position), _format_seconds(length))
            )
        return reported

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
            found = _NUMBER_ALONE.match(line.strip())
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
        Map a BlissFlixx action onto a cli verb, and confirm it on screen.

        Every action says what it did, in the player's own overlay, for a few
        seconds. That is the point: someone watching a film cannot read the log,
        and without any visible response there is no way to tell a button that
        worked from one that did nothing.

        It is driven by the actions themselves rather than by a timer. Polling
        once a second to keep a clock up to date turned out to be what stopped
        VLC's cli answering at all -- it went quiet within ten or twenty seconds
        of playback -- so a ticker was asking the player a question it could not
        keep answering. Asking once, right after the user does something, is
        both cheaper and answered far more reliably.

        The time does not tick. Showing the position when a seek happens says
        the seek landed, which is what it is for.
        """
        # Anything the user does ends the background searching. See _acted.
        self._acted = True
        if action in ("pause", "resume"):
            self._send_command("pause" if action == "pause" else "play")
            # With the time on it, because that is what pausing and un-pausing
            # is for: you stop to do something and come back to where you
            # stopped. "Paused" on its own said nothing about where.
            self._show_overlay(self._where("Paused" if action == "pause" else "Play"))
        elif action == "stop":
            self._send_command("quit")
        elif action in ("plus30", "minus30", "plus600", "minus600"):
            self._seek(_SEEK_SECONDS[action])
            self._report_progress()
        elif action in ("volup", "voldown"):
            # The step is a percentage; VLC counts in its own units.
            step = round(_VOLUME_MAX * int(self.opt("volume_step")) / 100)
            if action == "voldown":
                step = -step
            # Floored at zero, as it always was: repeatedly holding voldown
            # must not walk the level into negatives.
            level = max(0, self._current_volume() + step)
            self._set_volume(level)
            self._show_overlay("Volume %d%%" % _volume_percent(level))
        elif action == "hide_subtitle":
            # -1 is VLC's "disabled", and it is always in the listing.
            self._send_command("strack " + str(_TRACK_DISABLED))
            self._show_overlay("[off] subtitle")
        elif action == "show_subtitle":
            self._show_track("strack")
            self._show_overlay(
                _track_label(
                    "subtitle",
                    self._track_choice.get("strack"),
                    self._track_language("strack"),
                )
            )
        elif action == "next_subtitle":
            self._step_track("strack", +1)
            self._show_overlay(
                _track_label(
                    "subtitle",
                    self._track_choice.get("strack"),
                    self._track_language("strack"),
                )
            )
        elif action == "prev_subtitle":
            self._step_track("strack", -1)
            self._show_overlay(
                _track_label(
                    "subtitle",
                    self._track_choice.get("strack"),
                    self._track_language("strack"),
                )
            )
        elif action == "next_audio":
            self._step_track("atrack", +1)
            self._show_overlay(
                _track_label(
                    "audio",
                    self._track_choice.get("atrack"),
                    self._track_language("atrack"),
                )
            )
        elif action == "prev_audio":
            self._step_track("atrack", -1)
            self._show_overlay(
                _track_label(
                    "audio",
                    self._track_choice.get("atrack"),
                    self._track_language("atrack"),
                )
            )

    def _show_overlay(self, text):
        """
        Put a line of text on the picture, for a few seconds.

        Nothing here is timed or polled: the message is written when the user
        does something, and marq's own --marq-timeout takes it away again, so the
        screen is not left littered with confirmation of a pause from a quarter
        of an hour ago. An empty string is never written; see control().
        """
        if not self._overlay_wanted():
            return
        self._write_overlay(text)

    def _write_overlay(self, text):
        """
        Rewrite the file marq re-reads, atomically.

        marq reads it on a timer, so a half-written file would show a truncated
        message. Written to a temporary file and renamed, which is atomic on the
        same filesystem.

        A failed write is retried rather than given up on. It used to warn once
        and then stay silent for the rest of the session, so a single early
        failure -- the directory not existing yet, most likely -- left the
        overlay permanently dead while everything carried on looking healthy. The
        warning is throttled instead of latched.
        """
        if text == self._overlay_shown:
            return
        try:
            os.makedirs(os.path.dirname(MARQ_FILE), exist_ok=True)
            tmp = MARQ_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.replace(tmp, MARQ_FILE)
            self._overlay_shown = text
            self._overlay_failures = 0
        except OSError as exc:
            self._overlay_failures += 1
            # Warn early and then occasionally, not every time: this runs on
            # every action, and a line a second would be its own noise.
            if self._overlay_failures in (1, 60):
                cherrypy.log(
                    "could not write the VLC overlay file: %s (%s)" % (MARQ_FILE, exc)
                )

    def _overlay_wanted(self):
        return str(self.opt("osd_overlay")).lower() not in ("0", "false", "", "none")

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
            if self.killing or self._acted:
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
        Ask for the track listing and pull the ids and names out of it.

        Replies come back on the shared stdout, so this reads the same queue
        _send_command does and filters out the player's own logging.
        """
        lines = self._send_and_collect(command)
        if lines is None:
            return []
        ids = []
        names = {}
        for line in lines:
            found = _TRACK_ID.match(line.strip())
            if found:
                track_id = int(found.group(1))
                ids.append(track_id)
                # "Disable" is VLC's name for the -1 entry, not a language.
                if track_id >= 0:
                    names[track_id] = _track_language(line)
        if ids:
            self._track_listing[command] = ids
            self._track_names[command] = names
        # Log the listing whether or not anything matched. A command that was
        # sent but matched nothing looks exactly like one that was never sent --
        # both log nothing at all -- and that is what made the unanchored
        # pattern hard to find: the output was plainly arriving and simply
        # being ignored.
        cherrypy.log("VLC CLI: " + command + " -> " + _summarise_reply(lines))
        return self._track_listing.get(command, [])

    def _track_language(self, command):
        """
        The language of the chosen track, or None when VLC did not name it.
        """
        chosen = self._track_choice.get(command)
        if chosen is None or chosen < 0:
            return None
        return self._track_names.get(command, {}).get(chosen)

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

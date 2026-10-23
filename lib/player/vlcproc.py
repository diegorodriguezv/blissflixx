"""
VLC backend.

VLC is the recommended player on current Raspberry Pi OS. Hardware H.264 decode,
HDMI audio and subtitles all work without the expensive GStreamer compositing
path, which makes it the most capable of the options.

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
import secrets
import socket
import subprocess
import threading
import time
from queue import Empty, Queue

import cherrypy
import requests

from .backend import (
    CAP_AUDIO_TRACK,
    CAP_PAUSE,
    CAP_SEEK,
    CAP_STOP,
    CAP_SUBTITLE_DELAY,
    CAP_SUBTITLES,
    CAP_VOLUME,
    PlayerBackend,
)
from .processpipe import _TERMINATE_GRACE, TMP_DIR, ProcessException, _start_thread
from .pflixproc import drop_buffer_cache

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


def _track_parts(line):
    """
    The two halves of a listing line: what VLC calls the track, and its language.

        |  4 - Dubtitle - [English]
        |  3 - Track 1 - [English]
        | 40 - Simplified - [Chinese]

    Both matter and neither is enough. The name carries what the track *is* --
    SDH, Dubtitle, Simplified, a forced narration -- and the language is the
    only thing that tells thirty generically named tracks apart. A release with
    four English subtitles is unreadable with one of the two.
    """
    # The whole line after the id, not _TRACK_NAME's first segment: that regex
    # stops at the first " - ", which is exactly where the language starts, so
    # every track with a name came back as "Track 1" with no language at all.
    found = _TRACK_LINE.match(line.strip())
    if not found:
        return None, None
    rest = re.sub(r"\s*\*\s*$", "", found.group(2)).strip()
    bracketed = re.search(r"\[([^\]]+)\]\s*$", rest)
    plain = re.sub(r"\s*-?\s*\[[^\]]*\]\s*$", "", rest).strip()
    language = bracketed.group(1).strip() if bracketed else None
    return plain or None, language or None


#: VLC's own name for a track it has no title for: "Track 4".
_GENERIC_TRACK = re.compile(r"^Track \d+$")


def _without_language(name, language):
    """
    What is left of a track's name once the language is taken out of it.

        English (CC)          -> CC
        Simplified Chinese    -> Simplified
        Latin American Spanish -> Latin American

    Releases name their tracks in both orders -- "Chinese (Simplified)" and
    "Simplified Chinese" -- and the language is in the brackets on the listing
    line either way. Removing it is what keeps "Chinese (Simplified Chinese)"
    from happening when the name already says it.
    """
    pattern = re.compile(r"(?<!\w)" + re.escape(language) + r"(?!\w)", re.IGNORECASE)
    rest = re.sub(r"\s+", " ", pattern.sub(" ", name)).strip()
    rest = rest.strip(" -\u2013\u2014\u00b7:,[]")
    if rest.startswith("(") and rest.endswith(")") and rest.count("(") == 1:
        rest = rest[1:-1].strip()
    return rest.strip(" -\u2013\u2014\u00b7:,()[]")


def _track_display(name, language):
    """
    One line that says what a track is: "English (SDH)", "Chinese
    (Simplified)", "English", "Commentary".

    The language comes first and the rest is the variant in brackets, because
    the language is what is being chosen between and the variant is what tells
    two tracks in the same language apart. VLC's generic "Track 4" is dropped
    when a language is known: it is the player counting, not a name.
    """
    name = (name or "").strip()
    language = (language or "").strip()
    if name and language and _GENERIC_TRACK.match(name):
        name = None
    if name and language:
        name = _without_language(name, language)
        if name and name.lower() != language.lower():
            return "%s (%s)" % (language, name)
    return language or name or ""


#: A whole line of a track listing: "| 4 - Dubtitle - [English] *".
#:
#: _TRACK_NAME stops at the first " - " because it was written for the part
#: before the language; this keeps the rest so the language can be read too.
_TRACK_LINE = re.compile(r"^\|?\s*(-?\d+)\s+-\s*(.*?)\s*(\*)?$")

_TRACK_ID = re.compile(r"^\|?\s*(-?\d+)\s+-")
#: How long to keep asking for a subtitle track once the player is up. The
#: listing is empty until VLC has opened the media, and a torrent being streamed
#: over http can take a while to start producing.
_SUBTITLE_WAIT_TIMEOUT = 60
#: How many times to ask how long the film is before giving up and showing the
#: title alone, and how long to wait between asks.
#:
#: A handful, not a poll. Asking every second or two for a minute is what stops
#: this interface answering anything at all, taking the reply to the user's own
#: seeks with it. VLC can take fifteen or twenty seconds to report a length, so
#: the count has to cover that -- but it is a bounded few questions, not a
#: standing question, and it stops the moment the user touches anything.
#: How long to keep asking for a total length, as the gap before each question.
#:
#: The gaps grow. The only thing worth waiting for is a torrent, and a torrent's
#: length appears once enough of the file has arrived to read it -- measured on
#: one at between twenty and forty seconds, well past the twelve this used to
#: allow, which is why a torrent showed its name and never a total.
#:
#: Backing off as it goes keeps the number of questions down while the window
#: goes up: eight over about a minute and a half, against five over twelve
#: seconds before. This cli interface stops answering when it is questioned
#: continuously, and the reply to the user's own seeks dies with it -- which is
#: why the early questions are the cheap ones and the long gaps come last.
_TITLE_QUESTION_GAPS = (2, 3, 5, 8, 12, 15, 20, 25)
#: How long a confirmation stays up. Shorter than the opening title, which is
#: worth reading properly, and longer than marq's own --marq-timeout could give
#: both. marq has one timeout for the whole session, so the confirmations are
#: taken away by hand instead -- see _expire_overlay.
_CONFIRM_HOLD = 5
#: How long a seek is given to start delivering frames again before it is
#: called a stall. Long enough for VLC to re-prime after a jump, short enough
#: that the confirmation has not been sitting there looking stuck itself.
#:
#: Measured on rpi with --input-fast-seek, the decoder is producing frames again
#: within about two seconds of a seek. Without it, the count stays dead flat
#: for good, so this is not the difference between slow and stuck.
_SEEK_SETTLE = 2.5
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

#: One line of VLC's own `stats` answer, the running count of frames the video
#: decoder has produced. This is the only thing that says whether the picture is
#: actually moving. get_time does not: VLC's input clock is a wall clock and
#: keeps advancing when the decoder has stopped, which is why a frozen film used
#: to report a fresh elapsed time and read as a seek that had worked.
#:
#: "video decoded" rather than the "frames displayed" printed next to it. That
#: one counts what reached the video output, so it stays at zero whenever the
#: vout has no display to put them on -- which is the case on a Pi running
#: headless, where drm_vout cannot get a lease and says so, and where the film is
#: playing perfectly well. Reading that counter would report every seek on such
#: a box as a stall. The decoder's own count is the one that means what we mean.
#:
#: The reply is the statistical tree the cli interface prints, one counter per
#: line with a pipe in front of it:
#:
#:     +-[Video Decoding]
#:     | video decoded    :      268
#:     | frames displayed :      224
#:
#: The pipe is optional because the interface writes the block with a prompt in
#: front of it about as often as not.
_DECODED_FRAMES = re.compile(
    r"^(?:\s*>\s*)*\|?\s*video decoded\s*:\s*([\d,]+)", re.IGNORECASE
)

#: The same counter for sound, which is what a stream with no picture has. A
#: radio channel decodes audio and never a frame of video.
_DECODED_AUDIO = re.compile(
    r"^(?:\s*>\s*)*\|?\s*audio decoded\s*:\s*([\d,]+)", re.IGNORECASE
)


def _free_port():
    """
    A port nothing is listening on, for the player's http interface.

    Bound and released rather than guessed: the interface has to be told a
    number before it starts, and two plays in a row cannot be told the same one.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


#: How long the subtitle inside a torrent is given to arrive. It is a few KB,
#: but it is behind the same peers as the film, and a film that is barely
#: arriving has a subtitle that is barely arriving. Nothing waits on this: the
#: film is playing while it is asked for.
_SUBS_FETCH_TIMEOUT = 300

#: How often to ask whether anything has been decoded, while waiting for it.
_FRAME_POLL = 1.5

#: How long to keep asking. Long enough for a slow torrent to find peers, short
#: enough that a dead one stops costing commands. The bar keeps counting after
#: this; it just stops asking the player.
_FRAME_WAIT_TIMEOUT = 15 * 60


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


#: VLC's own volume scale. Its maximum is 512, not 100. The cli has no command
#: to read the level back, so the level in force is tracked from the last one set.
_VOLUME_SCALE = 512

#: VLC's own 100%: 256 on its 0-512 scale. VLC's default is 256, so this is the
#: level it uses when nothing has been set, and it is what a viewer means by
#: "full" -- no software gain applied to the stream, no chance of clipping it.
_VOLUME_NOMINAL = 256

#: How far above VLC's 100% the buttons may go: 120%.
#:
#: A film opens at 100%, so this is headroom rather than the usual setting -- for
#: a quiet source or a room with noise. It is a ceiling and not a recommendation:
#: the distortion that came before was not a film recorded too loud but this
#: player opening at twice normal level, and there is nothing past 120% that a
#: film needs.
#:
#: Enforced in _set_volume() rather than only through the volume_max setting,
#: because an install that already has the old 512 saved keeps it, and a setting
#: left over from the era when 512 was called 100% would otherwise put that
#: distortion straight back.
_VOLUME_MAX_PERCENT = 120

#: What the player starts at: VLC's 100%. Applied at readiness; see
#: _apply_start_volume.
_VOLUME_START_PERCENT = 100


def _volume_units(percent):
    """
    VLC's 0-512 level for a percentage of VLC's own 100%.

    The percentage is what this player tracks and what the overlay shows, and
    this is the conversion to VLC's units on the way to the cli. Converting here
    rather than accumulating a step in units is what keeps the numbers honest:
    a tenth of 256 is 25.6, so stepping 26 units at a time is 10.16% each time
    and every press walked further off the percentage grid. The overlay read

        100%  89%  79%  69%  59%  49%  39%  29%  18%  8%  0%

    for ten presses of volume down. Rounding happens once, here, and is at most
    half a unit -- 0.2% of full scale, well below hearing.
    """
    units = round(percent * _VOLUME_NOMINAL / 100)
    return max(0, min(units, _VOLUME_SCALE))


#: The same ceiling in VLC's units, for the volume_max setting. 120% is 307.
_VOLUME_MAX = _volume_units(_VOLUME_MAX_PERCENT)

#: How far VLC moves its subtitle delay per hotkey press. Not a choice: it is
#: what the hotkey does, there is no finer control, and no way to ask for an
#: amount directly -- so a step that is not a multiple of this is rounded to
#: whole presses and the overlay reports the rounded figure. Measured on the Pi:
#: fifteen presses put the delay at 0.75s and eight of the other way took 0.50s
#: back down, which is 50ms each, and negative values are allowed.
_SUBDELAY_QUANTUM = 0.05


def _subtitle_delay_presses(step):
    """
    How many hotkey presses one press of the button is worth.

    Rounded rather than truncated, and never zero: a step smaller than VLC's own
    50ms still has to move something rather than silently doing nothing, which
    would be the button-that-does-nothing this whole mechanism exists to avoid.
    """
    try:
        presses = int(round(float(step) / _SUBDELAY_QUANTUM))
    except (TypeError, ValueError):
        return 1
    return max(1, presses)


def _track_label(name, chosen, language=None):
    """
    What the overlay says after a track change.

    "[on] English" rather than "Subtitle English", because that is what it is:
    the state, then the language. With no language to name -- a track VLC does
    not describe, or one not yet chosen -- it falls back to the plain word, and
    never to a number, since "subtitle 0" would be claiming something not known.

    -1 is how VLC spells "none", so it reads as "off" rather than as a track
    numbered minus one.

    Off names the track the way on does, because the caller can say which track
    was showing and VLC's Disable entry names nothing of its own: "[off] Track
    1" says what was switched off, where "[off] subtitle" says only that
    subtitles are off.

    name is never empty, which is what keeps the last substitution honest: with
    no track to name this falls back to it rather than leaving "[off] " with
    nothing after it, which is the one string marq cannot be given.
    """
    if chosen is None:
        return name
    return "[%s] %s" % ("on" if chosen >= 0 else "off", language or name)


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

    cvlc takes argv, so no shell is needed. It can read a
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
            CAP_SUBTITLE_DELAY,
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
        # Keyframe seeking, on by default here and off in VLC.
        #
        # Two seeks in quick succession -- pressing +30 twice -- stopped the film
        # dead on this box. The picture froze on its last frame while the elapsed
        # time carried on, because VLC's input clock keeps running when the
        # decoder does not, and a seek's confirmation reads that clock. VLC says
        # why:
        #
        #     main decoder error: Timestamp conversion failed
        #         (delay 1000000, buffering 100000, bound 9000000)
        #     main decoder error: Could not convert timestamp ... for FFmpeg
        #
        # Measured on rpi, cvlc with the arguments below, against an HTTP
        # stream, counting "video decoded" from VLC's own stats:
        #
        #     two seeks 0.3s apart    frozen 6/6
        #     two seeks 3s apart      frozen 6/6
        #     two seeks 0.3s apart,
        #         --input-fast-seek   frozen 0/6
        #
        # Two facts worth keeping. It takes two seeks -- a single one survived
        # every time -- so the throttle that looked like the fix is not: spacing
        # the clicks out froze just as reliably. And it is not the torrents: this
        # reproduces on an ordinary HTTP source. Fast seek goes to the nearest
        # keyframe instead of asking the decoder to resynchronise its clock,
        # which is the step that fails.
        #
        # The cost is precision: the position lands on the nearest keyframe, so
        # it can be up to a GOP out, and subtitles can be briefly out by that
        # much after a seek. For a thirty second jump on a film that is not
        # visible, which is why this is on rather than off.
        "fast_seek": "1",
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
        # Fifteen seconds rather than three. Three is enough to see a change
        # you have just made, and not enough to have read it from a sofa --
        # the last thing pressed stays up long enough to still be there when
        # you look back at the screen.
        "osd_timeout": "15000",
        # How far the subtitles sit above the bottom edge, in destination pixels.
        # VLC applies this to the subtitle region alone (vout_subpictures.c lifts
        # the region by y_margin), so it is the only thing that moves them.
        #
        # Nothing. VLC's default, and the lowest they can sit -- at 220 they
        # were about 40% up the picture, and at 24 still had a gap under them.
        # The overlay is at the top now, so nothing needs clearing below.
        "sub_margin": "0",
        # An alignment enum, not a 1-10 scale: marq passes this straight
        # through as the subpicture region's i_align. The values marq offers are
        # 0 center, 1 left, 2 right, 4 top, 8 bottom. Guessing at a scale put
        # the message on the right of the screen; 4 is the top edge, which is
        # what a confirmation belongs at and well clear of the subtitles.
        "osd_position": "4",
        # Big enough to read from a sofa. VLC measures this in pixels of the
        # source frame, so it scales with resolution: at 1080p 28px was small.
        # Zero, which is not "invisible" but "no absolute size". VLC then sizes
        # the text off the video itself, which is how it sizes its own subtitles.
        #
        # This was 84, a fixed count of pixels, and that is why the overlay was
        # absurd over a 480p stream, big at 720p and only slightly big at 1080p --
        # the same 84 pixels against three times the picture.
        #
        # With 0, marq leaves i_font_size unset, and VLC's ConvertToLiveSize()
        # falls through to the relative size:
        #
        #     if( p_style->i_font_size )        absolute, pixels
        #     else if ( p_style->f_font_relsize )
        #         i_font_size = area_height * p_style->f_font_relsize / 100;
        #
        # The relative size it reaches is 6.25 -- STYLE_DEFAULT_REL_FONT_SIZE, or
        # video height / 16 -- which is the same number VLC gives a subtitle with
        # no size of its own. Read in modules/text_renderer/freetype and
        # modules/spu/marq.c for VLC 3.0.
        #
        # --sub-text-scale then multiplies both by the same factor, so the
        # overlay and a subtitle stay matched.
        #
        # A number here still overrides it, for anyone who wants the text the
        # same size whatever they are watching.
        "osd_size": "0",
        "osd_opacity": "220",
        "start_timeout": _START_TIMEOUT,
        # As a percentage of VLC's own 100%, not a number of its units: the step
        # used to be 5, which was 5 of 512 -- about one percent, so the button
        # appeared to do nothing at all. It is converted on the way out.
        #
        # Ten rather than five because five was still too small to see from a
        # sofa: the confirmation went from "Volume 50%" to "Volume 55%" and
        # read as no change at all.
        "volume_step": 10,
        "volume_max": _VOLUME_MAX,
        # How far one press of the subtitle-delay buttons moves the timing.
        # Quarter of a second, the same as mpv's, because the buttons are the
        # same buttons.
        #
        # VLC's own granularity is 50ms and there is no finer control, so a
        # step is rounded to whole presses and the overlay reports what was
        # actually sent. This being a setting rather than a constant is what
        # makes the finer grid reachable: quarter-second steps cannot land on a
        # film that is out by 0.4s, but a 0.05 step can.
        "subtitle_delay_step": 0.25,
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
        #: Whether VLC has said it has something to decode. False while the
        #: source is connected and silent; see waiting().
        self._media_seen = False
        self._startup_errors = []
        # Last track id chosen per interface verb, so stepping does not have to
        # re-read the listing every time. See _step_track.
        self._track_choice = {}
        self._track_listing = {}
        # Track id to language, from the listing the ids came from; the listing
        # is the only place a track's language appears.
        self._track_names = {}
        # The last real track that was showing, per interface verb. VLC's
        # "Disable" entry (-1) names nothing of its own, so a press that lands
        # on it has to reach back to this to say "[off] Track 1" rather than
        # "[off] subtitle". See _showing_track_name.
        self._track_showing = {}
        # The subtitle delay in force, in seconds. VLC's cli cannot read its own
        # back -- the delay is an input variable and the command table has no
        # verb for it -- so this is what was sent, accumulated. The presses are
        # the only thing that moves it and the offset lives on the input, so a
        # seek and a track change leave the two agreeing; a new film gets a new
        # input and a new player, and both start at zero.
        self._subtitle_delay = 0.0
        # Whether the player has answered the listing for this verb at all. A
        # film with no tracks answers with an empty listing, so "answered, and
        # there was nothing there" has to be told from "never asked" -- only the
        # first may be shown as "no subtitles". See _no_tracks.
        self._tracks_answered = {}
        # The input this stage was asked to play, remembered so that a failure
        # line can be told apart from one about a slave stream. Set in start().
        self._input_url = None
        # The loopback control port and its password. Chosen in start(), not in
        # build_command(), which is pure so it can be tested without a player.
        self._http_port = None
        self._http_password = None
        # Whether this stage has already said, in its own words, why it failed.
        # A message that says what happened needs no log tail after it; see
        # _add_output_to_error.
        self._failure_reported = False
        # What is playing, for the announcement at the start.
        self._title = ""
        # Whether we have paused it. VLC's cli can report the state but not set
        # it, so this is ours; see control().
        self._paused = False
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
        # the old queue.
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

        Always rewritten, not just when it is missing. It outlives the film,
        because the player does not delete it, so it was still holding the last
        message of the film before -- a confirmation of a pause, read at the
        start of the next one, which looked like something the new film had said
        about itself. It is emptied here as well as on stop, so a film started
        after a crash or a power cut does not inherit the last words of the one
        before it.
        """
        if not self._overlay_wanted():
            return
        try:
            os.makedirs(os.path.dirname(MARQ_FILE), exist_ok=True)
            with open(MARQ_FILE, "w", encoding="utf-8") as handle:
                handle.write(" ")
        except OSError as exc:
            # Not fatal: _write_overlay creates it on the first action and warns
            # if it cannot. This only saves the ticks before then.
            cherrypy.log("could not create the VLC overlay file: %s" % exc)

    #: What VLC says when the media it was given cannot be opened. Not one of
    #: _ERROR_MARKERS, which are about the interface itself failing to come up.
    _CANNOT_OPEN = "unable to open the MRL"

    #: What the video output says when it cannot put a frame on the screen. The
    #: Pi's contiguous video memory is 256MB and the display takes it out of the
    #: same pool as the decoder, so a box that has played a lot can run out: the
    #: film decodes, the screen stays black, and every frame fails to commit.
    #: Nothing recovers it but a restart, so it is worth saying rather than
    #: leaving a black screen that looks like a slow torrent.
    _NO_DISPLAY = "Atomic commit failed"

    def _observe_output(self, line):
        """
        Every line the player writes, and the two that mean something.

        VLC exits when it cannot open its input, and --play-and-exit makes that
        read exactly like the film ending: a playlist moved on to the next item
        without a word, and a film played on its own went back to the page with
        nothing said at all. Recorded here as this stage's error, which is what
        the pipe reports -- "Skipped <title>: Could not open the stream" in a
        run, and the message on its own otherwise.

        The other is the opposite failure, the one with no line of its own: the
        source is connected and nothing is arriving. That is recorded here as
        media having opened when VLC says it has a demuxer or a decoder, which
        is what waiting() reports the absence of.

        Only once the interface is up: before that _ready() is reading these
        same lines and has a better message of its own, VLC's reason included.

        And the failure is only for the input this stage was asked to play. VLC
        says the same thing about a slave stream, and the second half of a
        two-stream source failing is not the film failing.
        """
        if self._ready_seen and self._NO_DISPLAY in line and not self.has_error():
            self._set_error(
                "The display could not keep up with the film: the Pi is out of "
                "video memory. Restarting it clears this."
            )
            self._failure_reported = True
        elif (
            self._ready_seen
            and self._CANNOT_OPEN in line
            and self._input_url
            and self._input_url in line
            and not self.has_error()
        ):
            self._set_error("Could not open the stream")
            self._failure_reported = True
        self._replies.put(line)

    def waiting(self):
        """
        Whether the interface is up and the picture has not started.

        True from the moment the cli is ready until the decoder has produced a
        frame, which for a film is a second or two and for a torrent with no
        peers is never. See _wait_for_media.
        """
        return self._ready_seen and not self._media_seen

    def _add_torrent_subtitles(self):
        """
        Fetch the subtitles that live inside the torrent, when they can be had,
        and hand them to the player.

        Not before the film starts. peerflix answers a request for a file whose
        pieces are not here yet by waiting, so fetching these before the film
        meant waiting on the swarm twice: the fetch timed out after twenty
        seconds and the film played without them, which is what "Play With
        Subtitles just plays the video" was on a torrent still finding peers.

        So the film starts and these arrive whenever the pieces do -- through
        VLC's own addsubtitle, mid-play, which is the one way a subtitle can
        join a film already running. All of them are added; the first is the one
        selected, because the list arrives most probable first and the rest are
        tracks beside it for NEXT SUBS.

        One at a time, in order: which one is selected depends on which arrives
        first, and three threads racing would decide that by luck.
        """
        chosen = None
        for wanted in getattr(self, "_subtitles_wanted", []):
            if self.killing:
                return
            path = self._fetch_subtitle_file(wanted)
            if path is None:
                continue
            # The media has to be the one playing: addsubtitle applies to the
            # current input, and a command that arrives before the first frame
            # is lost.
            deadline = time.time() + 30
            while not self._media_seen and time.time() < deadline:
                if self.killing:
                    return
                time.sleep(0.5)
            if not self._add_subtitle_over_http(path):
                cherrypy.log("player would not take the subtitle: " + path)
                continue
            cherrypy.log("subtitle added: " + path)
            if chosen is None:
                tracks = [t for t in self._track_ids("strack") if t != _TRACK_DISABLED]
                if tracks:
                    chosen = tracks[-1]
        if chosen is not None:
            self._track_choice["strack"] = chosen
            self._remember_track("strack", chosen)
            self._send_command("strack " + str(chosen))

    def _fetch_subtitle_file(self, wanted):
        """One subtitle file out of the swarm, saved, or None if it will not come."""
        url = wanted.get("url")
        if not url:
            return None
        try:
            r = requests.get(url, timeout=_SUBS_FETCH_TIMEOUT)
            if r.status_code >= 300:
                raise ValueError("HTTP " + str(r.status_code))
            if not r.content:
                raise ValueError("empty reply")
        except Exception as exc:
            cherrypy.log("could not fetch the subtitle %s: %s" % (url, exc))
            return None
        name = os.path.basename(wanted.get("name") or "subtitle")
        path = os.path.join(TMP_DIR, re.sub(r"[^\w.\-]", "_", name))
        try:
            with open(path, "wb") as handle:
                handle.write(r.content)
        except OSError as exc:
            cherrypy.log("could not save the subtitle: %s" % exc)
            return None
        cherrypy.log("subtitle fetched: " + path)
        return path

    def add_subtitles(self, chosen, lang=None):
        """
        Put subtitles on the film that is already playing.

        The panel's button, for a film that turned out to have nothing usable:
        the alternative is to start it again, and on a torrent that means
        re-verifying its download -- minutes of watching lost to add a subtitle.
        """
        chosen = list(chosen or [])
        if not chosen:
            return False
        import json as _json

        from .subsproc import GETSUBS_PATH

        wanted = _json.dumps({"lang": lang or "eng", "files": chosen})
        try:
            out = subprocess.check_output(
                [GETSUBS_PATH, lang or "eng", "--choosens", wanted],
                stderr=subprocess.STDOUT,
                timeout=90,
            )
            found = _json.loads(out.decode("utf-8"))
        except Exception as exc:
            cherrypy.log("could not download the chosen subtitles: %s" % exc)
            return False
        paths = found.get("filenames") or []
        # The listing before, so the tracks this added can be told from the ones
        # already there. They are appended, and the search sorts by rating, so
        # the first new one is the best of them -- which is the one to show.
        before = set(self._track_ids("strack"))
        added = 0
        for path in paths:
            if self._add_subtitle_over_http(path):
                added += 1
        if not added:
            return False
        tracks = []
        for _ in range(10):
            tracks = [t for t in self._track_ids("strack") if t != _TRACK_DISABLED]
            if [t for t in tracks if t not in before]:
                break
            time.sleep(0.2)
        fresh = [t for t in tracks if t not in before]
        pick = fresh[0] if fresh else (tracks[-1] if tracks else None)
        if pick is not None:
            self._track_choice["strack"] = pick
            self._remember_track("strack", pick)
            self._send_command("strack " + str(pick))
        cherrypy.log("added %d subtitle(s) from the panel" % added)
        self._show_overlay("Subtitles added", hold=_CONFIRM_HOLD)
        return True

    def _add_subtitle_over_http(self, path):
        """
        Add a subtitle to the film that is playing.

        VLC 3's cli has no verb for it: "sub-add" is answered with

            libvlc's "sub-add" is not a command

        and --sub-file only exists at start, which is the whole problem -- the
        file may not be readable until long after the film is. The lua http
        interface does have addsubtitle, so it is started alongside the cli on
        a loopback port and used exactly once, for this. See build_command.
        """
        port = getattr(self, "_http_port", None)
        if port is None:
            return False
        try:
            # Encoded here rather than by requests: it turns a space into "+",
            # and the http interface reads the value back without turning "+"
            # into a space again -- VLC then looks for "Coyote+Vs.+Acme" on
            # disk and finds nothing. A percent-encoded space survives.
            from urllib.parse import quote

            r = requests.get(
                "http://127.0.0.1:%d/requests/status.json"
                "?command=addsubtitle&val=%s" % (port, quote(path, safe="/")),
                auth=("", self._http_password),
                timeout=10,
            )
        except Exception as exc:
            cherrypy.log("could not add the subtitle over http: %s" % exc)
            return False
        return r.status_code < 300

    def _wait_for_media(self):
        """
        Wait until the player has actually decoded something.

        The interface being up is not the film playing, and neither is the
        container being open: a torrent that sends its header and then stalls
        leaves VLC with a demuxer, a video output and nothing to show, so the
        count disappeared and the screen stayed black. Frames decoded is the
        only reading that means the picture rather than the plumbing, and it is
        the same counter the seek machinery trusts.

        Asked for on a timer, which this player avoids -- but there is no event
        to hang this on, and the wait is the whole point. Bounded, and it stops
        asking the moment a frame arrives.
        """
        deadline = time.time() + _FRAME_WAIT_TIMEOUT
        while time.time() < deadline:
            if self.killing or self._media_seen:
                return
            proc = getattr(self, "proc", None)
            if proc is None or proc.poll() is not None:
                return
            if self._frames_seen():
                self._media_seen = True
                cherrypy.log("media decoded: the picture has started")
                return
            time.sleep(_FRAME_POLL)

    def _frames_seen(self):
        """Whether VLC has decoded any frame at all, sound or picture."""
        for line in self._send_and_collect("stats") or []:
            line = line.strip()
            for pattern in (_DECODED_FRAMES, _DECODED_AUDIO):
                found = pattern.match(line)
                if not found:
                    continue
                try:
                    if int(found.group(1).replace(",", "")) > 0:
                        return True
                except ValueError:
                    pass
        return False

    def _add_output_to_error(self, tail):
        """
        Leave an error this stage already put in words alone.

        Everything else folds the process's last lines into its error, because a
        player that died without saying why leaves nothing else to go on. Here
        the message is the whole story, and what the tail would add is the
        shutdown noise that follows it --

            main playlist: end of playlist, exiting
            [cli] lua interface error: ... Interrupted.

        -- which turns a one-line message into something nobody reads to the end.
        """
        if self._failure_reported:
            return
        super()._add_output_to_error(tail)

    def _reset_replies(self):
        self._replies = Queue()
        self.on_output_line = self._observe_output
        # The cli interface has no volume query, so it is tracked from the last
        # set point. It is tracked as a percentage of VLC's own 100%, not in
        # VLC's 0-512 units, because that is the grid the buttons step on and
        # the grid the overlay shows; converting to units happens only on the way
        # out, in _set_volume. It starts at the level _apply_start_volume puts on
        # the player, so the two agree from the first frame instead of being
        # reconciled later.
        self._volume_percent = _VOLUME_START_PERCENT
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
        # Taken here because this is where the args arrive, and build_command
        # stays pure -- a stage that read it later could pick up the last film's
        # name if the same instance were reused.
        self._title = args.get("title") or ""
        # The subtitle inside the torrent is an address to be fetched once the
        # film is playing, not a file to hand VLC now. Taken here for the same
        # reason the title is: this is where the args arrive.
        self._subtitles_wanted = args.get("subtitles_wanted") or []
        # The http interface is what can add a subtitle to a film already
        # playing: the cli has no verb for it (VLC 3 answers "sub-add" is not a
        # command), and --sub-file only exists at start.
        #
        # Its port is chosen in start(), not here: build_command is documented
        # as pure so it can be tested without a player, and a port chosen here
        # would make every call return a different argv.
        # Taken from yt-dlp rather than worked out here, because there is no way
        # to work it out here. VLC's get_length answers for a live stream too,
        # and what it answers is the size of the sliding window -- it was 11 on
        # one IPTV channel and 13 a couple of seconds later. A number that
        # counts up is not a length, but there is no way to tell that apart from
        # a real one without polling it repeatedly, which is what used to stop
        # the cli interface answering at all.
        self._live = args.get("is_live") is True
        cmd = [self.opt("binary")]
        cmd += list(self.opt("extra_args"))
        cmd += self._osd_args()
        cmd += [
            # alsa first, then adummy, so a failure to open the sound card
            # costs the sound and not the film.
            #
            # Switching audio tracks tears the output down and builds it again,
            # and a burst of those switches makes it fail to reopen:
            #
            #     main audio output error: too low audio sample frequency (0)
            #     main decoder error: failed to create audio output
            #
            # With nothing left, the video stalls waiting on a clock that is
            # never going to arrive -- the picture freezes on its last frame
            # while the controls keep answering, because they never needed the
            # sound to work. VLC takes a comma-separated list here and uses the
            # first that opens, so a dead card means silence rather than a
            # frozen screen.
            "--aout=alsa,adummy",
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
            # Not --video-title-show: for a torrent that is not a title at all
            # but the stream URL, "http://192.168.1.119:9696/", which says
            # nothing to anyone watching. The title is ours to draw, below.
            "--no-video-title-show",
            "--play-and-exit",
        ]
        # See the note on fast_seek: two seeks in a row used to wedge the
        # decoder. Added here rather than in the list above because it is the
        # one flag that is genuinely optional -- the rest are this box's
        # measured working invocation.
        if str(self.opt("fast_seek")).lower() not in ("0", "false", "", "none"):
            cmd.append("--input-fast-seek")
        if self._http_port is not None:
            cmd.append("--extraintf=http")
            cmd.append("--http-host=127.0.0.1")
            cmd.append("--http-port=" + str(self._http_port))
            cmd.append("--http-password=" + self._http_password)
        if "subtitles" in args:
            # A list when the person picked several from a search, a single path
            # when one was fetched for them.
            #
            # Only the first goes in as --sub-file: that option holds one path
            # and a second one replaces it, which is what "two sub-files, one
            # track" was. The rest go in as slave inputs, which VLC adds to the
            # same film as further subtitle tracks -- measured on the Pi, two
            # sub-files gave one track, a sub-file with a slave gave two.
            chosen = args["subtitles"]
            if isinstance(chosen, str):
                chosen = [chosen]
            if chosen:
                cmd.append("--sub-file=" + chosen[0])
                for one in chosen[1:]:
                    cmd.append("--input-slave=" + one)
        if args.get("slave"):
            # The sound, when the source serves it separately from the picture.
            #
            # Above 360p YouTube has no stream carrying both, so a one-URL
            # pipeline could never show a film in HD. VLC plays two inputs at
            # once: this one and the slave. Measured on a Pi 4 with 1080p H.264:
            # hardware decoding, no dropped frames, and in sync -- including
            # after a seek.
            #
            # VLC calls the option experimental, which is why it is used for
            # exactly the case that needs it and nothing else: a muxed source
            # still arrives as one URL and plays as it always did.
            cmd.append("--input-slave=" + args["slave"])
        cmd.append(args["outfile"])
        return cmd

    def name(self):
        return "vlc"

    def start(self, args):
        """
        Give the player its own control port before it is started.

        The http interface is what can add a subtitle to a film already
        playing -- the cli has no verb for it -- and the control panel can ask
        for one at any moment, so every play has it. Loopback only, a port
        nothing else is using, and a password of its own that lives as long as
        the player and dies with it.

        The input is remembered here too: the failure line names the URL that
        could not be opened, and that is the only way to tell the main input
        from a slave stream, since VLC says the same thing about both. Kept out
        of build_command(), which is meant to be pure so it can be tested
        without a player.
        """
        self._http_port = _free_port()
        self._http_password = secrets.token_urlsafe(16)
        self._input_url = args.get("outfile")
        # Evict the torrent buffer's file pages before the player allocates.
        # The decoder's setup needs large contiguous pieces of the display
        # memory's region, and the verifier's reads fragment it; what was
        # dropped after the last play plus what has accumulated since goes now,
        # so the first allocations land on whole blocks. See drop_buffer_cache.
        dropped = drop_buffer_cache()
        if dropped:
            cherrypy.log("evicted %d buffer files from page cache" % dropped)
        super().start(args)

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
                self._apply_start_volume()
                self._enable_embedded_subtitles()
                _start_thread(self._wait_for_media)
                _start_thread(self._show_opening_title)
                if getattr(self, "_subtitles_wanted", None):
                    _start_thread(self._add_torrent_subtitles)
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

    def _apply_start_volume(self):
        """
        Put the starting level on the player before the first frame.

        VLC was never told what volume to play at. It starts at whatever it
        last saved -- and it saves, because volume-save defaults on -- so the
        level carried over from the previous session, or from another player on
        the same card. There is no --gain/--volume on the command line that the
        cli agrees with, and no cli verb to read the level back, so the code
        tracked its own idea of it in _reset_replies() and only ever sent a
        command when somebody pressed volume up or down.

        That left the first seconds of every film at a level nobody chose. It
        read as no audio at all: the picture was fine, the controls answered,
        and pressing volume up made the sound appear -- which is a maddening
        thing to have to do on every film.

        So the level the player says it is at is now also the level VLC is at.
        Both come from _VOLUME_START_PERCENT, so what the interface shows and
        what comes out of the speakers cannot disagree.
        """
        self._set_volume(_VOLUME_START_PERCENT)

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

    def _force_quit(self, grace=_TERMINATE_GRACE):
        """
        Make "quit" actually stop the player, and say so when it did not.

        "quit" only works if VLC's cli interface is still servicing its stdin,
        and a stream it cannot get through demuxing is what stops it doing so:
        a broken transport stream fills the log with

            ts demux error: libdvbpsi error (PSI decoder): TS duplicate

        and the player never reaches the command. The stop was then reported
        as done and nothing stopped. The player stayed in its playing state,
        so every later film queued behind a process that was never going to
        move, which read as the whole interface freezing -- the stop button
        and the play button both appeared dead.

        So ask politely, then insist. The stage thread already reaps VLC and
        notices it exit; this only has to not let it keep running.
        """
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            cherrypy.log("VLC IGNORED QUIT, TERMINATING")
            self._terminate()

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

    def _send_commands(self, commands):
        """
        Write several commands at once and collect one reply.

        Only for commands that answer nothing, which is the burst of hotkey
        presses and nothing else. Each command costs _REPLY_SETTLE to collect on
        its own -- three tenths of a second for one that says nothing -- so five
        of them one after another would hold the request thread for a second and
        a half, and a subtitle-delay button that took that long would read as
        broken. Written together, the burst costs the same one settle.

        Holding the lock across the whole write is what keeps another thread's
        command from landing in the middle of it. The interface reads whole
        lines, and a burst of unpaced lines is applied in full: twenty of them
        moved the delay by exactly a second, measured.
        """
        with self._command_lock:
            proc = getattr(self, "proc", None)
            if proc is None or proc.poll() is not None:
                return False
            if not commands:
                return True
            payload = "".join(command + "\n" for command in commands)
            try:
                proc.stdin.write(payload.encode("utf-8"))
                proc.stdin.flush()
            except (OSError, ValueError):
                return False
            lines = self._collect_reply()
            # Logged the way a single command is, but counted rather than
            # repeated, so the log says what moved without five identical lines.
            cherrypy.log(
                "VLC CLI: %d x %s -> %s"
                % (len(commands), commands[0], _summarise_reply(lines))
            )
            return True

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

    def _report_progress(self, label=None):
        """
        Ask where we are and show it.

        One shot, called after a seek rather than on a timer. Reading the clock
        continuously is what stopped this player answering anything at all after
        a few seconds of playback; reading it once, when the user has just asked
        to move, is answered reliably.

        label is what to say instead if the player will not say where it is.
        """
        reported = self._read_position()
        if reported is None:
            # Say the action rather than nothing.
            #
            # A missing confirmation cannot be told from a button that did
            # nothing, which is the one thing this whole mechanism exists to
            # rule out. The player not saying where it is says nothing about
            # whether the seek was accepted -- a seek that lands on a paused
            # film, or one that arrives while the player is busy with another,
            # is still a seek somebody asked for.
            if label:
                self._show_overlay(label, hold=_CONFIRM_HOLD)
            return None
        position, length = reported
        self._show_overlay(
            "%s / %s" % (_format_clock(position), _format_clock(length)),
            hold=_CONFIRM_HOLD,
        )
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

    def _decoded_frames(self):
        """
        How many frames VLC's video decoder has produced, or None if it won't say.

        The one reading that distinguishes a film that is playing from a film
        that has stopped, which get_time cannot do. Both of those used to look
        identical from here: the clock advanced, the picture did not.

        Asked for once, either side of a seek, and never on a timer. Polling the
        cli interface is what stopped it answering at all, ten or twenty seconds
        into playback, and that lesson is expensive enough to have been written
        down twice already. This is the same shape as the one-shot read that
        replaced the timer, only it counts frames instead of seconds.
        """
        for line in self._send_and_collect("stats") or []:
            found = _DECODED_FRAMES.match(line.strip())
            if found:
                try:
                    return int(found.group(1))
                except ValueError:
                    return None
        return None

    def _stalled(self, seconds):
        """
        Move the film, then check whether anything came back.

        Returns True when the seek went somewhere and the picture never
        restarted. A seek used to be confirmed from the clock alone, so a frozen
        film reported its new position as though the jump had worked -- the
        confirmation was the thing that hid the fault, not the fault itself.

        Two seeks in a row used to cause exactly this. A single seek has never
        been observed to do it. If it happens again, the film is now saying so
        on screen instead of quietly looking fine.

        The position goes up straight away, before the settle is waited out.
        VLC 3 draws no marker of its own for a seek on this box -- measured, the
        screen stays clean -- so this is the only feedback a ±30 s press has,
        and it is worth having on screen while the picture catches up rather
        than two and a half seconds later. A stall replaces it below; that is
        the one thing the message cannot claim.

        None rather than True when the count cannot be read either side of the
        seek. A player that will not answer is not evidence of a stall, and
        reporting one on that basis would be worse than the original lie: it
        would cry wolf over a film that was playing.
        """
        before = self._decoded_frames()
        self._seek(seconds)
        self._report_progress(label="Seek")
        if before is None:
            # Nothing to compare against, so there is nothing to wait for.
            return None
        time.sleep(_SEEK_SETTLE)
        after = self._decoded_frames()
        if after is None or after > before:
            return None
        # A paused film decodes nothing and that is correct -- it is meant to be
        # showing a still frame -- so this is only a stall if it was meant to be
        # moving.
        if self._paused:
            return None
        cherrypy.log(
            "VLC SEEK STALLED: %+d seconds, no frames decoded in %s seconds"
            % (seconds, _format_seconds(_SEEK_SETTLE))
        )
        self._show_overlay(self._where("Stalled"), hold=_CONFIRM_HOLD)
        return True

    def _seek(self, seconds):
        """
        Move the film, clamped to the film.

        VLC's cli routes seek through common.seek in share/lua/modules/common.lua:

            if string.sub(value,1,1) == "+" or string.sub(value,1,1) == "-" then
                vlc.var.set(input,"time",vlc.var.get(input,"time") + pos)
            else
                vlc.var.set(input,"time",pos)
            end

        and neither branch clamps. A time past the end is not refused: the
        demuxer runs to EOF, flashes a second or two of the beginning, and ends
        the input -- minutes of frozen picture and then the next film in a
        playlist, which is what "+10 minutes" near the end looked like.

        So the arithmetic is done here and sent as an absolute time, which is
        what lets it be clamped. The sign still matters and its absence was once
        a real bug: an unsigned value is absolute, so "plus30" jumped to 00:30 of
        the film instead of moving thirty seconds on. That bug is why the delta
        used to be passed straight through; it is now turned into an absolute
        target deliberately, and the target is what the player is given.

        When the player will not say where it is or how long it is, the delta is
        passed through unchanged. Blind is what happened before, and a press is
        better served approximately than refused.
        """
        target = self._seek_target(seconds)
        if target is None:
            return self._send_command("seek %+d" % seconds)
        return self._send_command("seek %d" % target)

    def _seek_target(self, seconds):
        """
        Where the seek should land, or None when the player will not say.

        Clamped to the film, and to one second short of its end. Exactly the
        length is an immediate end-of-input, so "+30" five seconds from the end
        would end the film rather than play its last stretch -- the press would
        mean "finish this" rather than "move on a bit". Stopping a second short
        leaves the end reachable by watching, which is how a film is meant to
        end.
        """
        reported = self._read_position()
        if reported is None:
            return None
        position, length = reported
        if length is None or length <= 0:
            return max(0.0, position + seconds)
        return max(0.0, min(position + seconds, length - 1))

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

        The time does not tick. Showing the position after a seek says where the
        film is, which is most of what is wanted -- but it does not on its own
        say the seek landed, because VLC's clock keeps running when the decoder
        has stopped, so a frozen film reported its new position perfectly
        happily. The position is shown because frames started arriving again,
        not instead of finding out. See _stalled().
        """
        # Anything the user does ends the background searching. See _acted.
        self._acted = True
        if action in ("pause", "resume"):
            # Both are a toggle here, and which word arrives does not say what
            # the player did.
            #
            # The interface sends "pause" for its PLAY button as well as its
            # PAUSE one -- it decides which to send by its own idea of the
            # state, which can disagree with the player's -- and VLC's pause
            # verb toggles. So "pause" arrives when the film is already paused
            # and resumes it, and the confirmation said "Pause" as it did the
            # opposite. It also meant no way to reach the film with VLC's state
            # known to differ from ours.
            #
            # So one verb, and the label says what the toggle actually did,
            # worked out from the state we last put it in.
            now_paused = not self._paused
            # Read and show before the toggle, not after. Asked for on the
            # grounds that a message written once the picture has stopped is not
            # one you can rely on being seen; this way it is on screen first
            # and the film then stops behind it. The position does not change
            # when the film is paused, so reading it first costs nothing.
            self._show_overlay(
                self._where("Pause" if now_paused else "Play"), hold=_CONFIRM_HOLD
            )
            self._send_command("pause")
            self._paused = now_paused
            # With the time on it, because that is what pausing and un-pausing
            # is for: you stop to do something and come back to where you

        elif action == "mute":
            # A hotkey, not a volume change: the cli has no mute verb, and
            # setting the volume to zero would lose the level to come back to.
            # VLC draws its own OSD for it, so there is no marquee here.
            #
            # The name is VLC's action id, not the word: the hotkey is
            # "key-vol-mute", the same way the delay hotkeys are
            # "key-subdelay-up". "mute" is not one, and the press was silently
            # ignored -- the overlay said "Muted" over a film that was not.
            self._send_command("key key-vol-mute")
        elif action == "stop":
            self._send_command("quit")
            self._force_quit()
        elif action in ("plus30", "minus30", "plus600", "minus600"):
            self._stalled(_SEEK_SECONDS[action])
        elif action in ("volup", "voldown"):
            # Stepped in percent, not in VLC's units, and that is the whole fix.
            # A tenth of 256 is 25.6, so a unit step had to be rounded to 26 --
            # which is 10.16%, not 10% -- so each press pushed the level a little
            # further off the grid the overlay reads from, and ten presses down
            # from 100% walked 100 89 79 69 59 49 39 29 18 8 0 instead of
            # 100 90 80 ... 0. Converting to units happens once, on the way out.
            step = int(self.opt("volume_step"))
            if action == "voldown":
                step = -step
            self._set_volume(self._current_volume() + step)
            # Read back rather than computed: _set_volume clamps, and the number
            # on the overlay is the level that was actually set -- so pressing up
            # at the ceiling says 120%, not a larger number that was refused.
            self._show_overlay(
                "Volume %d%%" % self._current_volume(), hold=_CONFIRM_HOLD
            )
        elif action == "hide_subtitle":
            # -1 is VLC's "disabled", and it is always in the listing.
            #
            # The listing is read first because that is the only thing that says
            # whether this film has subtitles to hide at all. Without it a film
            # that has none answers "[off] subtitle", which claims a track was
            # switched off when there was never one to switch off -- and it
            # would depend on the startup subtitle search having got as far as
            # asking, which is not something this press can assume. Every other
            # track action reads the listing for the same reason.
            self._track_ids("strack")
            self._send_command("strack " + str(_TRACK_DISABLED))
            # Recorded, not just sent. A step moves relative to the choice, so
            # leaving the choice on the track that was just switched off made
            # "next sub" step from a track the player is no longer on -- on a
            # one-track film it wrapped straight back to the Disable entry and
            # could not turn subtitles on at all. The track to come back to is
            # remembered separately, in _track_showing; see _show_track.
            self._track_choice["strack"] = _TRACK_DISABLED
            self._show_overlay(self._subtitle_overlay(), hold=_CONFIRM_HOLD)
        elif action == "show_subtitle":
            self._show_track("strack")
            self._show_overlay(self._subtitle_overlay(), hold=_CONFIRM_HOLD)
        elif action == "next_subtitle":
            self._step_track("strack", +1)
            self._show_overlay(self._subtitle_overlay(), hold=_CONFIRM_HOLD)
        elif action == "prev_subtitle":
            self._step_track("strack", -1)
            self._show_overlay(self._subtitle_overlay(), hold=_CONFIRM_HOLD)
        elif action == "next_audio":
            self._step_track("atrack", +1)
            self._show_overlay(self._audio_label(), hold=_CONFIRM_HOLD)
        elif action == "prev_audio":
            self._step_track("atrack", -1)
            self._show_overlay(self._audio_label(), hold=_CONFIRM_HOLD)
        elif action in ("subplus", "subminus"):
            # VLC draws its own OSD for the delay hotkeys -- measured on the Pi
            # -- so a confirmation of ours would sit on top of the player's.
            # Only a refusal is said here: that is a press which moved nothing,
            # and which the player therefore says nothing about.
            note = self._subtitle_delay_message(1 if action == "subplus" else -1)
            if note:
                self._show_overlay(note, hold=_CONFIRM_HOLD)

    def _show_overlay(self, text, hold=None):
        """
        Put a line of text on the picture, for a few seconds.

        Nothing here is timed or polled: the message is written when the user
        does something, and it goes away again on its own, so the screen is not
        left littered with confirmation of a pause from a quarter of an hour ago.
        An empty string is never written; see control().

        hold, when given, is how many seconds this one should stay rather than
        marq's own --marq-timeout. Needed because that is a single value for the
        whole session, and the opening title wants longer than a confirmation
        does.
        """
        if not self._overlay_wanted():
            return
        self._write_overlay(text)
        if hold:
            _start_thread(self._expire_overlay, text, hold)

    def _subtitle_label(self):
        """
        What the overlay says after a subtitle change.

        "no subtitles" when the player has answered and the film has none. Every
        subtitle button is then a no-op, and the bare word "subtitle" that came
        back instead said nothing about why -- it read as a state rather than as
        an absence.

        The state comes from the choice, which hide_subtitle records like any
        other change. VLC's Disable entry names nothing of its own, so when the
        choice is off the track to name is the one that was showing.
        """
        if self._no_tracks("strack"):
            return "no subtitles"
        chosen = self._track_choice.get("strack")
        if chosen is not None and chosen < 0:
            # The state, not the track that was showing. "subtitles off" is the
            # whole of what the press did; a name after it reads as a choice
            # that is still on.
            return "subtitles off"
        return _track_label(
            "subtitle",
            chosen,
            self._track_name("strack", chosen or self._track_showing.get("strack")),
        )

    def _audio_label(self):
        """
        What the overlay says after an audio change: the track's name alone.

        No "[on]": there is no hide-audio button, so the prefix said nothing the
        press had not just said itself. The track name is the whole answer, and
        the language is already in it.
        """
        if self._no_tracks("atrack"):
            return "no audio"
        chosen = self._track_choice.get("atrack")
        return (
            self._track_name("atrack", chosen or self._track_showing.get("atrack"))
            or "audio"
        )

    def _subtitle_delay_message(self, direction):
        """
        Why a subtitle-delay press could not move anything, or None when it did.

        None is the ordinary answer, and it is not shown. VLC's own OSD reports
        the press and the offset -- the -250/+250 ms buttons are hotkeys, and the
        player says what they did -- so a marquee of ours would repeat it in a
        second, larger message.

        Something is said only when the press moved nothing and the player will
        therefore say nothing either. The delay belongs to the selected track
        rather than to the film: measured on the Pi, four presses with the track
        switched off left VLC's delay at zero. Moving it there would be a change
        nobody could see, and the word for a film with no subtitle tracks is the
        same one the subtitle buttons use -- it is the same fact about the film.
        """
        if self._no_tracks("strack"):
            return "no subtitles"
        chosen = self._track_choice.get("strack")
        if chosen is None or chosen < 0:
            return "Subtitles are off"
        self._shift_subtitle_delay(direction)
        return None

    def _shift_subtitle_delay(self, direction):
        """
        Move the subtitle timing a step and report where it landed.

        VLC's cli has no verb for this -- the command table has no sub-delay --
        so it is done the way the desktop control does it, by simulating the
        hotkey. "key" is not in the printed help either, but it is in the lua,
        and a hotkey press is a hotkey press: each one moves the delay VLC
        actually renders subtitles with by exactly 50ms, negative values are
        allowed, and the offset survives a seek and a track change. It is that
        variable and not a copy of it -- read back over VLC's http interface,
        six presses showed 0.30s and it was still 0.30s after a seek, and after
        hiding and showing the subtitles.

        Returns the delay in force now, in seconds. The step is rounded to whole
        presses, so this is not always the number the setting names.
        """
        presses = _subtitle_delay_presses(self.opt("subtitle_delay_step"))
        hotkey = "key key-subdelay-" + ("up" if direction > 0 else "down")
        if not self._send_commands([hotkey] * presses):
            # The commands never reached the player, so its delay did not move
            # and neither does what this says is in force.
            return self._subtitle_delay
        moved = presses * _SUBDELAY_QUANTUM
        self._subtitle_delay = round(self._subtitle_delay + direction * moved, 3)
        return self._subtitle_delay

    def _expire_overlay(self, text, hold):
        """
        Take a message away after its own, shorter, time is up.

        Writes a space rather than nothing. marq reads the file with getline(),
        which returns -1 at end of file and which marq reports as "Invalid
        argument" -- once per refresh tick, for as long as playback lasts. That
        is what a confirmation that cleared itself used to leave behind.

        Guarded on the text still being the one on screen, so a message that has
        already been replaced by a later action is not wiped by the earlier
        one's timer.
        """
        time.sleep(hold)
        if self._overlay_shown == text:
            self._write_overlay(" ")

    def _write_overlay(self, text):
        """
        Rewrite the file marq re-reads, atomically.

        The text is escaped for strftime first: see the note below.

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
        # marq runs the text through strftime before drawing it, so a percent
        # sign in a film's name is a format sequence: "100% Entertainment" loses
        # characters, and "%Y" becomes the year. Doubling them is what strftime
        # reads back as one.
        text = text.replace("%", "%%")
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
        first. This is what "subtitles are mandatory" means in practice.

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
        if getattr(self, "_subtitles_wanted", None):
            # A subtitle is coming out of the torrent, and that one decides what
            # is shown. This waiter picks the first track it sees, which raced
            # the torrent's own selection and sometimes won.
            return
        _start_thread(self._wait_for_subtitles, args)

    def _show_opening_title(self):
        """
        Say what started, and how long it is, once the film is up.

        What is being played, with its total length. Not the position: at the
        start that is 0:00 by definition, and a position reads as though
        something had gone wrong.

        The name goes up straight away and the total is added to it later, if and
        when the player can tell us. That order matters. It used to ask first and
        fall back to the name alone, which left a torrent with nothing on screen
        at all for its first twelve seconds -- and a torrent then showed its name
        and nothing else, for good, because its length does not exist until enough
        of the file has arrived to read it. Measured on one: unknown at twenty
        seconds, 25:36 by forty.

        So the waiting is real and it is why the questions are spread out and
        then spread further. Asking every second or two for a minute is what
        stopped this interface answering anything at all, and with it the reply
        to the user's own seeks. Asking at most eight times over a minute and a
        half, with the gap growing each time, is a fraction of that and still
        finds the length of a torrent. And because the name is already up, the
        wait costs nothing to be looked at.

        A live stream has no total to report and is not asked for one -- see
        build_command() -- so it shows the name and finishes.

        Stops at once if the user does anything -- see _acted.
        """
        if not self._overlay_wanted():
            return
        title = self._title
        if not title:
            return
        if self._live:
            # Nothing to ask for and nothing to show but the name. See
            # build_command(): asking anyway would be a question whose answer is
            # discarded, and every needless question to this cli interface is a
            # chance for it to stop answering.
            self._show_overlay(title)
            return
        # Say what is playing before going looking for how long it is, so there
        # is always something on the picture while we wait.
        self._show_overlay(title)
        for gap in _TITLE_QUESTION_GAPS:
            if self.killing or self._acted:
                return
            proc = getattr(self, "proc", None)
            if proc is None or proc.poll() is not None:
                return
            time.sleep(gap)
            if self.killing or self._acted:
                return
            length = self._read_number("get_length")
            if length is not None and length > 0:
                self._length = length
                self._show_overlay("%s (%s)" % (title, _format_clock(length)))
                return
        # Never knew. The name is already up and stays up, which is the answer
        # this can give, not a blank screen.

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
                    "(subtitles are on by default)" % tracks[0]
                )
                self._track_choice["strack"] = tracks[0]
                self._remember_track("strack", tracks[0])
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
        last one showing is preferred, so show/hide/show returns to the same
        track rather than jumping somewhere else; otherwise the first track in
        the listing.

        The showing track comes from _track_showing rather than from the choice,
        because hiding records -1 in the choice -- a step from "off" has to move
        relative to the Disable entry, which is a different question from which
        track to come back to.

        This used to send "strack 0" on the assumption that 0 meant "the first
        one". It does not. A real listing on the Pi came back as

            | -1 - Disable
            | 2 - English (CC) - [English]

        so 0 is not a track at all, and asking for it turned subtitles back on
        for nobody.
        """
        target = self._track_choice.get(command)
        if target is None or target == _TRACK_DISABLED:
            target = self._track_showing.get(command)
            if target is None:
                tracks = self._track_listing.get(command) or self._track_ids(command)
                real = [t for t in tracks if t != _TRACK_DISABLED]
                if not real:
                    return False
                target = real[0]
            self._track_choice[command] = target
            self._remember_track(command, target)
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
        # Real tracks only: -1 is VLC's Disable, and stepping onto it turned
        # "next subtitle" from the last track into "subtitles off" -- a state
        # with its own button, and one that the next press then had to step back
        # out of. Audio has no hide button at all, so it must never land there.
        tracks = [t for t in self._track_ids(command) if t != _TRACK_DISABLED]
        if not tracks:
            return False
        current = self._track_choice.get(command)
        if current is None or current not in tracks:
            current = tracks[0]
        # Whether it was read from the choice or assumed to be the first real
        # track, this is the one that was showing, and it is what an "[off] ..."
        # label names when this step lands on the Disable entry. Audio is never
        # turned on at startup the way subtitles are, so without this the first
        # press has nothing to name.
        self._remember_track(command, current)
        index = tracks.index(current)
        target = tracks[(index + direction) % len(tracks)]
        self._track_choice[command] = target
        self._remember_track(command, target)
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
        # The player answered, which is not the same as having named a track. A
        # film with no subtitle tracks answers with the header and the footer of
        # an empty listing and nothing between them -- not even the "-1 - Disable"
        # that a film with tracks always carries -- so "answered, and there was
        # nothing there" is a thing this knows, and it is the only thing that
        # lets the overlay say "no subtitles".
        #
        # Set and never cleared. An answer, once heard, is not un-heard by a
        # later reply that came back with nothing in it, and the listing for a
        # film does not change while it is playing.
        if lines:
            self._tracks_answered[command] = True
        ids = []
        names = {}
        for line in lines:
            found = _TRACK_ID.match(line.strip())
            if found:
                track_id = int(found.group(1))
                ids.append(track_id)
                # "Disable" is VLC's name for the -1 entry, not a language.
                if track_id >= 0:
                    title, language = _track_parts(line)
                    names[track_id] = _track_display(title, language)
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

    def _track_name(self, command, track_id):
        """
        VLC's name for a track id, or None when it did not name it.

        None for -1 as well as for an id never listed. "Disable" is the state,
        not a track, and has no name of its own -- which is what makes
        _showing_track_name necessary.
        """
        if track_id is None or track_id < 0:
            return None
        return self._track_names.get(command, {}).get(track_id)

    def _showing_track_name(self, command):
        """
        The name of the last real track that was showing, or None.

        This outlives a choice of -1 on purpose: a press that lands on VLC's
        Disable entry, or one that hides the track that was on, leaves nothing
        chosen to name, and this is what there is to name instead.
        """
        return self._track_name(command, self._track_showing.get(command))

    def _remember_track(self, command, track_id):
        """
        Note the track that is now the one showing.

        Real tracks only. -1 is how VLC spells "none", so recording it would
        erase the very name an "[off] ..." label needs.
        """
        if track_id is not None and track_id >= 0:
            self._track_showing[command] = track_id

    def _subtitle_overlay(self):
        """
        What the subtitle buttons say.

        One track is not a list, and "off" is a state rather than a position, so
        both keep the one-line form: "[on] English (CC)". Two or more get the
        list around the current track, which is the only way to tell four
        English subtitles apart or to know how many there are to step through.
        """
        if self._no_tracks("strack"):
            return "no subtitles"
        chosen = self._track_choice.get("strack")
        ids = [i for i in (self._track_listing.get("strack") or []) if i >= 0]
        if chosen is None or chosen < 0 or len(ids) < 2:
            return self._subtitle_label()
        return self._track_window(ids, chosen)

    def _track_window(self, ids, current):
        """
        The tracks either side of the chosen one, and where in the list it is.

            Chinese (Simplified) \u25c2 English (Dubtitle) 2/39 \u25b8 English (SDH)

        One before and one after, wrapping around the ends so there is always
        both -- a list is clearer than a window that shrinks at the ends of the
        film's subtitle list, and the position already says where you are. The
        current track sits in the middle with its position and the total, an
        arrow pointing back to the one before and forward to the one after.

        One line, because the marquee lays out one line: \n and \r\n are read as
        the end of the text (MarqueeReadFile in marq.c reads the file with
        getline), and a long line wraps by itself.

        "Disable" is never in the ring: it is not a track, and the buttons that
        need it -- hide, and the next press out of it -- have their own way of
        saying so.
        """
        count = len(ids)
        position = ids.index(current)
        names = self._track_names.get("strack") or {}

        def display(one):
            return names.get(one) or ("Track %d" % one)

        here = "%s %d/%d" % (display(current), position + 1, count)
        if count < 2:
            return here
        before = display(ids[(position - 1) % count])
        after = display(ids[(position + 1) % count])
        # Arrows rather than brackets: they say which way the buttons step, and
        # a space alone let the three run together on a busy frame. Both are in
        # the fonts VLC falls back to on the Pi.
        return "%s \u25c2 %s \u25b8 %s" % (before, here, after)

    def _no_tracks(self, command):
        """
        True when the player has answered the listing and named no tracks.

        False when it named some, and false when it has not answered at all,
        which is what a listing never asked for looks like from here. That
        distinction is the whole point: "no subtitles" is a claim about the
        film, and a player that would not answer -- or one whose media is not
        open yet, when the listing comes back empty for that reason instead --
        is not evidence of one. Same rule as _stalled() returning None.

        The listing is filtered of -1, so a stream carrying nothing but VLC's
        own "Disable" entry counts as having no tracks, which is what it is.
        """
        if not self._tracks_answered.get(command, False):
            return False
        real = [t for t in self._track_listing.get(command, []) if t != _TRACK_DISABLED]
        return not real

    def _current_volume(self):
        """
        The level in force, as a percentage of VLC's own 100%.
        """
        return self._volume_percent

    def _set_volume(self, percent):
        """
        Put the volume at a percentage of VLC's own 100%, and remember it.

        Clamped to _VOLUME_MAX_PERCENT as well as to whatever the volume_max
        setting says, because an install that still has the old 512 saved would
        otherwise climb straight back to VLC's 200% and distort -- which is the
        bug the ceiling exists to stop. See its comment.

        The number kept is the clamped percentage rather than the unit level
        that was sent, so what the overlay reads back is the number it just
        showed, to the digit.
        """
        ceiling = min(
            _VOLUME_MAX_PERCENT,
            round(self.opt("volume_max") * 100 / _VOLUME_NOMINAL),
        )
        self._volume_percent = max(0, min(percent, ceiling))
        # "volume N", not "vol N": the verb is "volume", and there is no "vol".
        # The cli interface reports an unrecognised command by echoing the
        # prompt and saying nothing at all, which is indistinguishable from
        # success unless you know the verb list.
        return self._send_command("volume %d" % _volume_units(self._volume_percent))

    def stop(self):
        # Emptied before the player goes, so the next film starts with a blank
        # screen rather than the last words of this one. It is a file the player
        # does not remove, and marq reads whatever is in it at startup, so a
        # confirmation left over from a pause was shown at the start of the next
        # film -- read as though the new film had said it about itself.
        #
        # A space rather than nothing, for the reason everywhere else here: an
        # empty file is one marq reports as an error on every refresh tick.
        self._write_overlay(" ")
        self._overlay_shown = None
        # Ask the player to leave before the signal does.
        #
        # A quit through the interface it is already listening on is the exit
        # it knows how to tidy up after, and the process is still signalled
        # below if this does not land. (This was once thought to release the
        # Pi's video memory -- measured, ~145MB still gone after a play -- but
        # that loss is the torrent buffer's file pages sitting in the display
        # memory's region, not the player failing to exit: both exits in the
        # log are clean shuts, and evicting the pages recovers it. See
        # drop_buffer_cache in pflixproc.)
        self._send_command("quit")
        super().stop()

import json
import os
import sys

from ..locations import COOKIES_PATH
from . import nodejs, ythelper
from .processpipe import OUT_FILE, ExternalProcess, ProcessException

#: What this stage asks yt-dlp to print, and the label it prints with each.
#:
#: One line per stream rather than the full extraction JSON. That JSON includes
#: every automatic caption track yt-dlp knows about, and one YouTube documentary
#: here was 11 MB of subtitle metadata on a single line for the output tail to
#: buffer before it could be parsed.
#:
#: Labelled rather than positional because yt-dlp writes warnings of its own
#: between these lines, and because a field an extractor does not have comes out
#: as NA -- on its own line a bare NA is a whole readable answer meaning "this
#: one does not say", which is what it is.
_PRINTED = [
    # The film, when the source serves it as one stream.
    ("BFFILM", "%(url)#j"),
    # The picture and the sound, when it serves them separately. Which of the
    # three is real says what kind of source this is, and the last one printed
    # is the end of the answer -- see _ready().
    ("BFVIDEO", "%(requested_formats.0.url)#j"),
    ("BFAUDIO", "%(requested_formats.1.url)#j"),
    ("BFLIVE", "%(is_live)#j"),
]
_MARKERS = {marker for marker, _ in _PRINTED}


def _printed_url(value):
    """
    The URL out of one printed line, or None when there was not one.

    %(url)#j quotes the value as JSON; a field the extractor does not have comes
    out as NA, sometimes quoted. Both mean "no URL here" and the caller treats
    them the same way, as absent.
    """
    text = value.strip()
    if text.startswith('"') and text.endswith('"'):
        try:
            text = json.loads(text)
        except ValueError:
            return None
    if not text or text in ("NA", "null"):
        return None
    return text


def _printed_live(value):
    """
    Whether the film is live. Only a printed true means it is.

    A site whose extractor does not report the field prints NA, and that is an
    absence of information rather than a statement -- so it reads as not live,
    which keeps a film showing its length rather than losing it.
    """
    return value.strip() == "true"


class YoutubeDlProcess(ExternalProcess):
    def __init__(self, url):
        super().__init__()
        self.url = url
        # The printed direct URL can itself be several kilobytes long because signed
        # media URLs carry a large query string. The normal 2 KB partial cap would
        # truncate it before the newline, so give this stage room to keep the
        # whole line. Four megabytes is far above any single URL.
        self.partial_limit = 4 * 1024 * 1024

    def name(self):
        return "yt-dlp"

    def _get_cmd(self, args):
        self.args = args
        # A page that is not itself playable is exchanged for the media URL
        # before anything else looks at it, so skip_download, the format choice
        # and the URL handed to yt-dlp all agree about what is being played. See
        # ythelper.resolve().
        url = ythelper.resolve(self.url)
        cmd = [
            # The interpreter running this, not "python" from PATH.
            #
            # Those are not always the same one, and they were not: this stage
            # ran under whatever "python" happened to be first on PATH, so a
            # server started by hand used the system python's packages while a
            # server started by run.sh used the virtualenv's. yt-dlp's own code
            # came from the checkout either way -- its __main__ puts itself on
            # sys.path -- but everything it imports came from whichever
            # interpreter won, so the two machines had different dependencies
            # and neither had all of them.
            #
            # sys.executable cannot drift: blissflixx.py refuses to run outside
            # this checkout's virtualenv, so by the time this is reached there
            # is exactly one interpreter it could mean.
            sys.executable,
            "-m",
            "yt_dlp",
            # Which JavaScript runtime to use, if any. yt-dlp stopped assuming
            # one: with no --js-runtimes it looks for deno alone, and on a
            # machine without it, YouTube extraction is deprecated and returns
            # nothing at all.
            #
            # The path is the checkout's own node when configure.sh has unpacked
            # one, because yt-dlp refuses node below version 22 and the
            # distribution ships 20. quickjs is named after it as the fallback:
            # it is small and in the distribution, but it is an interpreter and
            # took four times as long to solve the same challenge (79s against
            # 19s, measured on a Pi 4).
            #
            # Safe to name runtimes that are not there: yt-dlp says "Ignoring
            # unsupported JavaScript runtime(s)" and carries on, so this cannot
            # turn a working machine into a broken one.
            "--js-runtimes",
            nodejs.node_runtime(),
            "--js-runtimes",
            "quickjs",
            "--no-part",
            "--no-continue",
            "--no-playlist",
            "--no-progress",
            "--output",
            OUT_FILE,
        ]

        if os.path.isfile(COOKIES_PATH):
            # A cookies file, when one has been put there.
            #
            # YouTube answers a Raspberry Pi with "sign in to confirm you're not
            # a bot", and that is not something a version of yt-dlp fixes: it
            # wants an account that is signed in. There is no browser on the box
            # to read one out of, so the only way to have one is to carry it
            # over -- a Netscape cookies.txt exported from a browser that is
            # signed in, put at COOKIES_PATH. See the README.
            #
            # Absent is the normal case, and then no flag is passed at all, so a
            # box that has never needed this behaves exactly as it did.
            cmd += ["--cookies", COOKIES_PATH]

        if ythelper.skip_download(url):
            cmd.append("--simulate")
            # Four labelled answers rather than one, because a source can be one
            # stream or two.
            #
            # Above 360p YouTube serves picture and sound separately, and for
            # some videos it has no muxed stream at all. VLC plays two inputs at
            # once (--input-slave), and that is what makes 1080p reachable: the
            # one-URL pipeline could only ever use a muxed stream. So the film's
            # URL and the sound's URL, and whichever are real are used -- see
            # _streams().
            #
            # Labelled rather than positional because yt-dlp writes warnings of
            # its own between these lines, and %(url) is NA for a two-stream
            # selection while the requested_formats pair are NA for a muxed one.
            # The label says which line is which, so nothing has to be guessed
            # from its shape.
            for marker, template in _PRINTED:
                cmd.append("--print")
                cmd.append(marker + ":" + template)

        cmd.append("--format")
        fmat = ythelper.get_format(url)
        if fmat is not None:
            cmd.append(fmat)
        else:
            cmd.append("best")
        cmd.append(url)
        return cmd

    def _ready(self):
        self.args["pid"] = self.proc.pid
        printed = {}
        while True:
            line = self._readline()
            if line.startswith("[download] Destination:"):
                self.args["outfile"] = OUT_FILE
                return self.args
            if line.startswith("ERROR:"):
                raise ProcessException(self._get_ytdl_err(line[7:]))
            marker, sep, value = line.partition(":")
            if not sep or marker not in _MARKERS:
                continue
            printed[marker] = value
            # The last line asked for is the end of the answer. Everything
            # before it can be missing and this still works.
            if marker == _PRINTED[-1][0]:
                return self._streams(printed)

    def _streams(self, printed):
        """
        Turn the printed answers into this stage's arguments.

        Two URLs means picture and sound are separate, and the player wants both:
        the film as its input and the sound as its slave. One URL means the
        source is a muxed stream and there is nothing to pair. Neither means the
        extractor reported no playable stream at all.

        The slave is put here rather than worked out by the player because this
        is where the two URLs exist, and because which of them is picture and
        which is sound is yt-dlp's answer, not a guess the player could make.
        """
        film = _printed_url(printed.get("BFFILM", ""))
        video = _printed_url(printed.get("BFVIDEO", ""))
        audio = _printed_url(printed.get("BFAUDIO", ""))
        if video and audio:
            self.args["outfile"] = video
            self.args["slave"] = audio
        elif film:
            self.args["outfile"] = film
        else:
            raise ProcessException("No URL in YTDL")
        self.args["is_live"] = _printed_live(printed.get("BFLIVE", ""))
        return self.args

    def _get_ytdl_err(self, msg):
        if msg.strip() == "":
            return
        idx = msg.find("YouTube said:")
        if idx > -1:
            msg = msg[idx + 14 :]

        idx = msg.find("Unsupported URL:")
        if idx > -1:
            msg = "Unsupported URL"

        idx = msg.find("is not a valid URL.")
        if idx > -1:
            msg = msg[: idx + 18]

        idx = msg.find("This video is no longer available")
        if idx > -1:
            msg = "No longer available"

        # Assume 403 is because wrong country
        idx = msg.find("HTTP Error 403: FORBIDDEN")
        if idx > -1:
            msg = "This video is not available in your country"

        idx = msg.find("ERROR:")
        if idx > -1:
            idx = msg.find(" ", idx)
            msg = msg[idx + 1 :]

        return msg

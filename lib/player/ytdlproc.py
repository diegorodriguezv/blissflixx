import json
import os
import sys

from ..locations import COOKIES_PATH
from . import ythelper
from .processpipe import OUT_FILE, ExternalProcess, ProcessException

#: How many lines to look through for the is_live answer. It is printed right
#: after the URL, so this only has to cover the player's own warnings landing
#: between the two.
_IS_LIVE_SCAN = 4


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
            # Two are named because neither alone is enough. yt-dlp refuses node
            # below version 22, and the node in Raspberry Pi OS is 20 -- so
            # naming node alone, which is what this did, bought nothing at all
            # on the Pi and said so only in a warning nobody reads until a video
            # fails. quickjs is the one configure.sh installs: it is small, it
            # is in the distribution, and the current build clears yt-dlp's
            # 2023-12-09 minimum.
            #
            # Safe to name runtimes that are not there: yt-dlp says "Ignoring
            # unsupported JavaScript runtime(s)" and carries on, so this cannot
            # turn a working machine into a broken one.
            "--js-runtimes",
            "node",
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
            # Two short lines rather than the full extraction JSON. That JSON
            # includes every automatic caption track yt-dlp knows about, and one
            # YouTube documentary here was 11 MB of subtitle metadata on a
            # single line for the output tail to buffer before it could be
            # parsed. Two fields is all this stage needs.
            #
            # Separate lines, not one object, because a field an extractor does
            # not have comes out as a bare NA -- and inside an object that makes
            # the whole line unparseable, so one missing field would take the URL
            # down with it. is_live is missing for some sites (xnxx does not
            # report it at all) and that is not a reason a film should stop
            # playing. On its own line a bare NA is a whole readable answer
            # meaning "this one does not say", which is what it is.
            cmd.append("--print")
            cmd.append("%(url)#j")
            cmd.append("--print")
            cmd.append("%(is_live)#j")

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
        while True:
            line = self._readline()
            if line.startswith("[download] Destination:"):
                self.args["outfile"] = OUT_FILE
                return self.args
            elif line.startswith('"'):
                # The --print %(url)#j form emits a JSON string rather than the
                # full extraction object.
                url = json.loads(line)
                if not url or url == "NA":
                    raise ProcessException("No URL in YTDL")
                self.args["outfile"] = url
                self.args["is_live"] = self._read_is_live()
                return self.args
            elif line.startswith("{"):
                obj = json.loads(line)
                if not "url" in obj:
                    if "requested_formats" in obj:
                        url = obj["requested_formats"][0]["url"]
                    else:
                        raise ProcessException("No URL in YTDL")
                else:
                    url = obj["url"]
                if not url or url == "NA":
                    raise ProcessException("No URL in YTDL")
                self.args["outfile"] = url
                self.args["is_live"] = obj.get("is_live") is True
                return self.args
            elif line == "NA" or line == "null":
                raise ProcessException("No URL in YTDL")
            elif line.startswith("ERROR:"):
                raise ProcessException(self._get_ytdl_err(line[7:]))

    def _read_is_live(self):
        """
        Read the line after the URL: whether this is a live stream.

        Only true means live. A site whose extractor does not report the field
        at all prints NA, and that is an absence of information rather than a
        statement -- so it reads as not live, which keeps a film showing its
        length rather than losing it.

        Scans a few lines rather than taking the very next one, because the
        player writes warnings of its own in between. Bounded, because a stream
        that reported a URL and nothing after it is not a reason to keep
        waiting: nothing is reported and the film plays.
        """
        for _ in range(_IS_LIVE_SCAN):
            line = self._readline()
            if line == "true":
                return True
            if line in ("false", "NA", "null"):
                return False
        return False

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

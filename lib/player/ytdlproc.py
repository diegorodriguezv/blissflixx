import json
import os

from ..locations import YTUBE_PATH
from . import ythelper
from .processpipe import OUT_FILE, ExternalProcess, ProcessException

YTDL_PATH = os.path.join(YTUBE_PATH, "yt_dlp")
YTDL_PATH = os.path.join(YTDL_PATH, "__main__.py")


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
            "python",
            YTDL_PATH,
            "--no-part",
            "--no-continue",
            "--no-playlist",
            "--no-progress",
            "--output",
            OUT_FILE,
        ]

        if ythelper.skip_download(url):
            cmd.append("--simulate")
            # Print the direct media URL, and whether this is a live stream, as
            # one small JSON object. Not the full extraction JSON: that includes
            # every automatic caption track yt-dlp knows about, and one YouTube
            # documentary here was 11 MB of subtitle metadata on a single line
            # for the output tail to buffer before it could be parsed. Two
            # fields is all this stage needs.
            #
            # The #j form rather than j. j emits the value raw, and a field the
            # extractor does not have comes out as bare NA -- which inside an
            # object is not JSON at all, and would turn a missing URL into an
            # unparseable line rather than the clear error it is. #j quotes
            # whatever it finds, so the line parses and the missing field is
            # still recognisable as missing.
            cmd.append("--print")
            cmd.append('{"url": %(url)#j, "is_live": %(is_live)#j}')

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
                # The --print %(url)j form emits a JSON string rather than the
                # full extraction object.
                url = json.loads(line)
                if not url:
                    raise ProcessException("No URL in YTDL")
                self.args["outfile"] = url
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
                # #j quotes a value the extractor did not have, so a missing URL
                # arrives as the string NA rather than as nothing.
                if not url or url == "NA":
                    raise ProcessException("No URL in YTDL")
                self.args["outfile"] = url
                # Whether this is live is not decoration: it is the only thing
                # that says a running total means anything. A live stream's
                # "length" is a sliding window and keeps counting up, so the
                # player stage has to be told not to put one on screen.
                self.args["is_live"] = obj.get("is_live") is True
                return self.args
            elif line == "NA" or line == "null":
                raise ProcessException("No URL in YTDL")
            elif line.startswith("ERROR:"):
                raise ProcessException(self._get_ytdl_err(line[7:]))

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

import json
import os

from ..locations import BIN_PATH
from .processpipe import ExternalProcess, ProcessException

GETSUBS_PATH = os.path.join(BIN_PATH, "getsubs.py")


class SubtitlesProcess(ExternalProcess):
    def __init__(self, subs):
        super().__init__()
        self.subs = subs
        self.subsfile = None

    def status_msg(self):
        return "FETCHING SUBTITLES"

    def name(self):
        return "subtitles"

    def _get_cmd(self, args):
        cmd = [GETSUBS_PATH, self.subs["lang"]]
        if "series" in self.subs:
            cmd = cmd + ["-t", self.subs["series"]]
            cmd = cmd + ["-s", self.subs["season"]]
            cmd = cmd + ["-e", self.subs["episode"]]
        else:
            cmd = cmd + ["-t", self.subs["title"]]
            if "year" in self.subs and self.subs["year"]:
                cmd = cmd + ["-y", self.subs["year"]]
            if "imdb" in self.subs and self.subs["imdb"]:
                cmd = cmd + ["-i", self.subs["imdb"]]
        return cmd

    def _ready(self):
        while True:
            line = self._readline()
            if line.startswith("{"):
                obj = json.loads(line)
                if "filename" in obj:
                    self.subsfile = obj["filename"]
                    return {"subtitles": self.subsfile}
                elif "error" in obj:
                    raise ProcessException(obj["error"])
                else:
                    raise ProcessException("No subtitles found")
            else:
                raise ProcessException("Subtitles died")

    def stop(self):
        # The subtitle file is kept. It used to be deleted on every stop, which
        # meant the next play downloaded it all over again -- for a file that is
        # tiny, over a connection that may well be the reason someone is
        # watching something at all.
        super().stop()

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
        # Every argument is a string. A year stored as a number -- which is what
        # a saved playlist holds, and what the old subtitle page could send --
        # used to reach the argv as an int, and the only place it was noticed was
        # the log line that joins the command: "sequence item 5: expected str
        # instance, int found", out of the reporting, which killed the stage's
        # thread. The film never started and the interface sat at LOADING STREAM
        # for as long as somebody was willing to wait.
        cmd = [GETSUBS_PATH, str(self.subs["lang"])]
        if "series" in self.subs:
            cmd = cmd + ["-t", str(self.subs["series"])]
            cmd = cmd + ["-s", str(self.subs["season"])]
            cmd = cmd + ["-e", str(self.subs["episode"])]
        else:
            cmd = cmd + ["-t", str(self.subs["title"])]
            if "year" in self.subs and self.subs["year"]:
                cmd = cmd + ["-y", str(self.subs["year"])]
            if "imdb" in self.subs and self.subs["imdb"]:
                cmd = cmd + ["-i", str(self.subs["imdb"])]
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


class ChosenSubtitlesProcess(ExternalProcess):
    """
    The subtitles picked from a search, rather than one chosen for the person.

    Fetched in one go, by id, and all of them handed to the player: a release
    often has a plain track and a hearing-impaired one, and having both on the
    film is the reason for being allowed to pick more than one.
    """

    def __init__(self, subs):
        super().__init__()
        self.subs = subs

    def status_msg(self):
        return "FETCHING SUBTITLES"

    def name(self):
        return "subtitles"

    def _get_cmd(self, args):
        wanted = {
            "lang": self.subs.get("lang") or "eng",
            "files": self.subs.get("chosen") or [],
        }
        return [GETSUBS_PATH, wanted["lang"], "--choosens", json.dumps(wanted)]

    def _ready(self):
        while True:
            line = self._readline()
            if not line.startswith("{"):
                raise ProcessException("Subtitles died")
            found = json.loads(line)
            if "filenames" in found:
                if not found["filenames"]:
                    raise ProcessException("No subtitles could be downloaded")
                return {"subtitles": found["filenames"]}
            if "error" in found:
                raise ProcessException(found["error"])
            raise ProcessException("No subtitles found")

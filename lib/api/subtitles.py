import json
import subprocess

from ..chanutils.chanutils import movie_title_year, series_season_episode
from ..player.subsproc import GETSUBS_PATH
from .common import ApiError


def _search_name(name):
    """
    What to search OpenSubtitles for, from a release name.

    The name on a torrent row is a file name -- quality, source, codec, group --
    and searching it finds nothing. The year is kept because it separates a film
    from its remake.
    """
    series = series_season_episode(name)
    if "episode" in series:
        return {
            "title": series["series"],
            "season": series["season"],
            "episode": series["episode"],
        }
    parsed = movie_title_year(name)
    found = {"title": parsed["title"]}
    if "year" in parsed:
        found["year"] = parsed["year"]
    return found


def search(name=None, lang=None, title=None, year=None):
    """
    The subtitles OpenSubtitles has for this, as a list to choose from.

    title and year may be given directly, from a page where the person has
    corrected them; otherwise they are read out of the release name.
    """
    if title is None:
        if not name:
            raise ApiError("A title or a release name is needed")
        wanted = _search_name(name)
        title = wanted["title"]
        year = year if year is not None else wanted.get("year")
        season = wanted.get("season")
        episode = wanted.get("episode")
    else:
        season = episode = None

    cmd = [GETSUBS_PATH, lang or "eng", "--list", "-t", title]
    if season and episode:
        cmd = cmd + ["-s", str(season), "-e", str(episode)]
    elif year:
        cmd = cmd + ["-y", str(year)]
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=90).decode(
            "utf-8"
        )
        found = json.loads(out)
    except subprocess.TimeoutExpired:
        raise ApiError(
            "OpenSubtitles did not answer in time", ApiError._SERVICE_UNAVAILABLE
        )
    except subprocess.CalledProcessError as exc:
        raise ApiError(
            "OpenSubtitles search failed: " + exc.output.decode("utf-8", "replace")
        )
    except ValueError:
        raise ApiError("OpenSubtitles answered with something unreadable")
    if isinstance(found, dict) and "error" in found:
        raise ApiError("OpenSubtitles: " + str(found["error"]))
    return {"title": title, "year": year, "results": found}

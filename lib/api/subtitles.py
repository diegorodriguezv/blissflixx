import json
import subprocess

from ..chanutils.chanutils import movie_title_year, release_kind, series_season_episode
from ..player.subsproc import GETSUBS_PATH
from .common import ApiError


def _search_name(name):
    """
    What to search OpenSubtitles for, from a release name.

    The name on a torrent row is a file name -- quality, source, codec, group --
    and searching it finds nothing. The year is kept because it separates a film
    from its remake. Packs (season/series/movie collections) are identified so
    the UI can guide per-file/per-episode subtitle selection instead of guessing.
    """
    info = release_kind(name)
    kind = info.get("kind")
    if kind == "episode":
        return {
            "title": info["series"],
            "season": info["season"],
            "episode": info["episode"],
            "kind": "episode",
        }
    if kind == "tv_pack":
        # Single season can be searched as the show (season hinted in title);
        # multi-season/complete packs are not one subtitle search.
        if "season" in info and not info.get("pack") and not info.get("seasons"):
            return {
                "title": info["series"],
                "season": info["season"],
                "kind": "tv_season",
            }
        return {
            "title": info.get("series") or "",
            "kind": "tv_pack",
            "seasons": info.get("seasons"),
            "season": info.get("season"),
            "pack": True,
        }
    if kind == "movie_pack":
        return {
            "title": info.get("title") or "",
            "year": info.get("year"),
            "kind": "movie_pack",
            "pack": True,
        }
    if kind == "movie":
        return {"title": info["title"], "year": info.get("year"), "kind": "movie"}
    # Fallback to legacy parsers.
    series = series_season_episode(name)
    if "episode" in series:
        return {
            "title": series["series"],
            "season": series["season"],
            "episode": series["episode"],
            "kind": "episode",
        }
    parsed = movie_title_year(name)
    found = {"title": parsed["title"], "kind": "movie"}
    if "year" in parsed:
        found["year"] = parsed["year"]
    return found


def search(name=None, lang=None, title=None, year=None):
    """
    The subtitles OpenSubtitles has for this, as a list to choose from.

    title and year may be given directly, from a page where the person has
    corrected them; otherwise they are read out of the release name.
    """
    wanted_kind = None
    pack = False
    season = episode = None
    if title is None:
        if not name:
            raise ApiError("A title or a release name is needed")
        wanted = _search_name(name)
        title = wanted.get("title") or (name or "")
        year = year if year is not None else wanted.get("year")
        season = wanted.get("season")
        episode = wanted.get("episode")
        wanted_kind = wanted.get("kind")
        pack = bool(wanted.get("pack"))
    else:
        season = episode = None

    # A multi-item pack is not a single subtitle target.
    if pack:
        return {
            "title": title,
            "year": year,
            "season": season,
            "seasons": wanted.get("seasons") if title is None else None,
            "kind": wanted_kind or "pack",
            "pack": True,
            "results": [],
        }

    search_title = title
    if wanted_kind == "tv_season" and season and not episode:
        # getsubs has no season-only mode; hint season in the query.
        search_title = "%s Season %d" % (title, season)

    cmd = [GETSUBS_PATH, lang or "eng", "--list", "-t", search_title]
    if season and episode:
        cmd = [GETSUBS_PATH, lang or "eng", "--list", "-t", title, "-s", str(season), "-e", str(episode)]
    elif year and wanted_kind not in ("tv_season", "episode"):
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
    return {
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "kind": wanted_kind,
        "results": found,
    }

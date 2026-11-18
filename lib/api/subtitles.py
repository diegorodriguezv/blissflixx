import json
import subprocess

import cherrypy

from ..chanutils.chanutils import movie_title_year, release_kind, series_season_episode
from ..chanutils.languages import LANGUAGES  # noqa: F401  (re-export)
from ..player.subsproc import GETSUBS_PATH
from ..settings import load, save
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
        out = subprocess.check_output(cmd, stderr=subprocess.STDOUT, timeout=300)
    except subprocess.TimeoutExpired:
        raise ApiError(
            "OpenSubtitles is taking too long to respond. Please try again.",
            ApiError._SERVICE_UNAVAILABLE,
        )
    except subprocess.CalledProcessError as exc:
        detail = exc.output.decode("utf-8", "replace").strip()
        if detail:
            cherrypy.log("OpenSubtitles search failed:\n" + detail)
        raise ApiError(
            "Could not search OpenSubtitles right now. Please try again.",
            ApiError._SERVICE_UNAVAILABLE,
        )
    detail_text = out.decode("utf-8", "replace")
    try:
        found = json.loads(detail_text)
    except Exception:
        if detail_text.strip():
            cherrypy.log("OpenSubtitles unreadable response:\n" + detail_text.strip())
        raise ApiError("OpenSubtitles returned an unreadable response. Please try again.")
    if isinstance(found, dict) and "error" in found:
        msg = str(found.get("error") or "Search failed").strip()
        raise ApiError("OpenSubtitles: " + msg)
    return {
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "kind": wanted_kind,
        "results": found,
    }


#: Where the global subtitle language lives. A channel may override it; a
#: subtitle search uses the language chosen for that search, not this one.
_GLOBAL = "subtitles"


def _language_choices():
    return [{"code": code, "name": name} for code, name in LANGUAGES]


def language():
    """The global subtitle language and the choices, for the settings page."""
    stored = load(_GLOBAL).get("lang") or "eng"
    if stored not in {code for code, _ in LANGUAGES}:
        stored = "eng"
    return {"lang": stored, "languages": _language_choices()}


def set_language(lang=None):
    """Set the language used by channels that do not choose their own."""
    if lang is None:
        raise ApiError("Subtitle language is missing")
    if lang not in {code for code, _ in LANGUAGES}:
        raise ApiError("Unknown subtitle language: " + str(lang))
    save(_GLOBAL, {"lang": lang})
    return language()

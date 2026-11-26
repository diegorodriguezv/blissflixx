import json
import os
import re
import subprocess

import cherrypy

from ..chanutils.chanutils import (
    movie_title_year,
    release_kind,
    series_season_episode,
    without_year,
)
from ..chanutils.languages import LANGUAGES  # noqa: F401  (re-export)
from ..player.subsproc import GETSUBS_PATH
from ..settings import load, save
from .common import ApiError




def _file_stem(name):
    """A file name without its folder or extension, ready to compare."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    return os.path.splitext(base)[0].strip().lower()


def _folder_of(name):
    """The torrent's own folder in a path, if the path has one."""
    parts = (name or "").replace("\\", "/").split("/")
    return parts[0].strip() if len(parts) > 1 else ""


def _words(name):
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def _mentions(name, needle):
    """Whether a name mentions a phrase, as whole words."""
    wanted = _words(needle)
    return bool(wanted) and (" " + wanted + " ") in (" " + _words(name) + " ")


def _folder_match(folder):
    """
    The title and year a torrent's folder names, for matching results.

    The folder is the release name inside the torrent ("The Pacific (2010)
    Mp4 1080p/..."), and a subtitle made for that release usually names the
    show and the year even when its exact file name differs.
    """
    if not folder:
        return "", None
    parsed = movie_title_year(folder)
    return parsed.get("title") or "", parsed.get("year")


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _search_name(name, context=None):
    """
    What to search OpenSubtitles for, from a release name and its context.

    The name on a torrent row is a file name -- quality, source, codec, group --
    and searching it finds nothing. The year is kept because it separates a film
    from its remake. Packs (season/series/movie collections) are identified so
    the UI can guide per-file/per-episode subtitle selection instead of guessing.

    `context` is what the row already knows: a channel's title/year/imdb, or a
    pack's series and the file's season/episode (see blissflixx/api/torrent.py). It
    fills what the name does not say, and wins when it says this is an episode
    of a series -- a file called "Episode 09.mp4" carries no series name, and a
    pack title with no season marker is read as a film by the heuristics.
    """
    context = context if isinstance(context, dict) else {}
    cseries = context.get("series")
    ctitle = context.get("title")
    cyear = _as_int(context.get("year"))
    cseason = _as_int(context.get("season"))
    cepisode = _as_int(context.get("episode"))
    cimdb = context.get("imdb")

    info = release_kind(name)
    kind = info.get("kind")

    # Only an episode number in the context says "this is an episode". A row in
    # a TV category carries its whole release name under "series" when the name
    # has no SxxEyy, and that must not turn a pack into one episode.
    if cseries and cepisode is not None:
        found = {"title": str(cseries), "kind": "episode"}
        if cseason is not None:
            found["season"] = cseason
        if cepisode is not None:
            found["episode"] = cepisode
        if cyear is not None:
            found["year"] = cyear
        if cimdb:
            found["imdb"] = cimdb
        return found

    if kind == "episode":
        found = {
            "title": without_year(info.get("series") or "") or str(ctitle or ""),
            "kind": "episode",
        }
        for key, fallback in (
            ("season", cseason),
            ("episode", cepisode),
            ("year", cyear),
        ):
            value = info.get(key)
            if value is None:
                value = fallback
            if value is not None:
                found[key] = value
        if "episode" in found and "season" not in found:
            # A part ("1of3") or an "Episode 09" is almost always the first
            # season; the page shows the number and lets it be corrected, and
            # the search is for one file rather than the whole series.
            found["season"] = 1
        if cimdb:
            found["imdb"] = cimdb
        return found
    if kind == "tv_pack":
        # Single season can be searched as the show (season hinted in title);
        # multi-season/complete packs are not one subtitle search.
        if "season" in info and not info.get("pack") and not info.get("seasons"):
            found = {
                "title": without_year(info["series"]),
                "season": info["season"],
                "kind": "tv_season",
            }
            if cimdb:
                found["imdb"] = cimdb
            return found
        found = {
            "title": without_year(info.get("series") or ""),
            "kind": "tv_pack",
            "seasons": info.get("seasons"),
            "season": info.get("season"),
            "pack": True,
        }
        if cimdb:
            found["imdb"] = cimdb
        return found
    if kind == "movie_pack":
        return {
            "title": info.get("title") or "",
            "year": info.get("year") if info.get("year") is not None else cyear,
            "kind": "movie_pack",
            "pack": True,
        }
    if kind == "movie":
        found = {"title": info.get("title") or str(ctitle or ""), "kind": "movie"}
        year = info.get("year")
        if year is None:
            year = cyear
        if year is not None:
            found["year"] = year
        if cimdb:
            found["imdb"] = cimdb
        return found
    # Fallback to legacy parsers.
    series = series_season_episode(name)
    if "episode" in series:
        found = {
            "title": series["series"],
            "season": series["season"],
            "episode": series["episode"],
            "kind": "episode",
        }
        if cimdb:
            found["imdb"] = cimdb
        return found
    parsed = movie_title_year(name)
    found = {
        "title": parsed.get("title") or str(ctitle or name or ""),
        "kind": "movie",
    }
    year = parsed.get("year")
    if year is None:
        year = cyear
    if year is not None:
        found["year"] = year
    if cimdb:
        found["imdb"] = cimdb
    return found


def search(
    name=None,
    lang=None,
    title=None,
    year=None,
    season=None,
    episode=None,
    imdb=None,
    context=None,
):
    """
    The subtitles OpenSubtitles has for this, as a list to choose from.

    title/year/season/episode/imdb may be given directly, from a page where the
    person has corrected them; otherwise they are read out of the release name,
    with `context` filling what it does not say.
    """
    wanted_kind = None
    pack = False
    if title is None:
        if not name:
            raise ApiError("A title or a release name is needed")
        wanted = _search_name(name, context)
        title = wanted.get("title") or (name or "")
        year = year if year is not None else wanted.get("year")
        season = season if season is not None else wanted.get("season")
        episode = episode if episode is not None else wanted.get("episode")
        imdb = imdb or wanted.get("imdb")
        wanted_kind = wanted.get("kind")
        pack = bool(wanted.get("pack"))
    else:
        # The page sends the corrected title and the fields beside it.
        if imdb is None and isinstance(context, dict):
            imdb = context.get("imdb")
        wanted_kind = "episode" if episode is not None else ("movie" if year else None)

    year = _as_int(year) if year is not None else None
    season = _as_int(season) if season is not None else None
    episode = _as_int(episode) if episode is not None else None
    if episode is not None:
        wanted_kind = "episode"
    elif season is not None and wanted_kind is None:
        wanted_kind = "tv_season"

    # A multi-item pack is not a single subtitle target.
    if pack:
        return {
            "title": title,
            "year": year,
            "season": season,
            "seasons": wanted.get("seasons") if pack else None,
            "kind": wanted_kind or "pack",
            "imdb": imdb,
            "pack": True,
            "results": [],
        }

    if not title and not imdb:
        raise ApiError("A title or a release name is needed")

    search_title = title
    if wanted_kind == "tv_season" and season and not episode:
        # getsubs has no season-only mode; hint season in the query.
        search_title = "%s Season %d" % (title, season)

    cmd = [GETSUBS_PATH, lang or "eng", "--list"]
    if search_title:
        cmd += ["-t", search_title]
    if imdb:
        cmd += ["-i", str(imdb)]
    if season is not None and episode is not None:
        cmd += ["-s", str(season), "-e", str(episode)]
    elif year and wanted_kind not in ("tv_season", "episode"):
        # The year is a film's; a season search names the season in the query
        # instead, and getsubs would read -y as "this is a film".
        cmd += ["-y", str(year)]
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
    # The strongest sign that a subtitle was made for this film is its name
    # being the film's name: "Show.S01E09.720p-GRP.srt" for
    # "Show.S01E09.720p-GRP.mkv". The extension differs by definition and case
    # is not worth losing a match over. A subtitle that names the title, or
    # the title and year of the torrent's folder, is a weaker but useful sign.
    wanted_stem = _file_stem(name)
    folder_title, folder_year = _folder_match(_folder_of(name))
    for item in found:
        sub_name = item.get("name")
        item["exact"] = bool(wanted_stem) and _file_stem(sub_name) == wanted_stem
        item["matches_title"] = _mentions(sub_name, title)
        item["matches_folder"] = bool(
            folder_year
            and _mentions(sub_name, folder_title)
            and _mentions(sub_name, str(folder_year))
        )
    return {
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "kind": wanted_kind,
        "imdb": imdb,
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

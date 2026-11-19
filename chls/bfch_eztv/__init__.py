"""
EZTV's listings and title search.

EZTV's JSON API answers by IMDb id: /api/get-torrents?imdb_id=1190634. It has
no title search, and the HTML search and show pages are behind Cloudflare on
every mirror. A title is therefore resolved to an id with TVMaze first
(api.tvmaze.com/search/shows), and a pasted tt... id skips that step. The rows
a search returns carry the id, so Add Subtitles can ask OpenSubtitles by id.
"""

import math
import re
from datetime import datetime, timezone

from lib.api.common import ApiError
from lib.api.torrent import TorrentPlayItem, icon_for_title
from lib.chanutils import get_json
from lib.playitem import PlayItemList


_BASE_API = "https://eztvx.to/api/get-torrents"

#: TVMaze maps a title to an IMDb id. EZTV's API answers by id only, so this is
#: the half of a title search that EZTV cannot do.
_TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows"

#: A pasted id, with or without the tt. EZTV wants the digits.
_IMDB_RE = re.compile(r"^(?:tt)?(\d{3,})$", re.IGNORECASE)

#: How many pages of one show's episodes a search reads. The API pages at 100,
#: so this is the newest 500 releases; long-running soaps have more, and the
#: oldest seasons are not worth five hundred rows of scrolling to reach.
_MAX_PAGES = 5

_FEEDLIST = [
    {"title": "Latest", "url": _BASE_API},
    {"title": "All Shows", "url": _BASE_API},
]


def name():
    return "EZTV"


def image():
    return "icon.png"


def description():
    return "EZTV Torrents Channel (<a target='_blank' href='https://eztvx.to'>https://eztvx.to</a>)."


def feedlist():
    return _FEEDLIST


def _item(t, imdb=None):
    """One torrent as a row, or None when it has no magnet to play."""
    magnet = t.get("magnet_url")
    if not magnet:
        return None
    title = (t.get("title") or t.get("filename") or "").strip()
    size_bytes = t.get("size_bytes") or 0
    try:
        size = "%.1f MB" % (int(size_bytes) / (1024 * 1024))
    except (TypeError, ValueError):
        size = "NA"
    seeds = t.get("seeds")
    if seeds is None:
        seeds = 0
    seeds = str(seeds)
    released_unix = t.get("date_released_unix")
    try:
        released = datetime.fromtimestamp(
            int(released_unix), tz=timezone.utc
        ).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError, OverflowError):
        released = "NA"
    subtitle = "Size: " + size
    subtitle = subtitle + ", Seeds: " + seeds
    subtitle = subtitle + ", Released: " + released
    # A search knows which show this is, so Add Subtitles can ask OpenSubtitles
    # by id instead of by the release name.
    subs = {"imdb": "tt" + str(imdb)} if imdb else None
    return TorrentPlayItem(
        title, icon_for_title(title, "/img/icons/tv.svg"), magnet, subtitle, subs=subs
    )


def _rows(torrents, imdb=None):
    results = PlayItemList()
    for t in torrents:
        item = _item(t, imdb=imdb)
        if item is not None:
            results.add(item)
    return results


def feed(idx):
    data = get_json(_BASE_API, params={"limit": 100, "page": idx + 1})
    return _rows(data.get("torrents", []) or [])


def _imdb_for(q):
    """
    The show to search, as (imdb digits, name).

    A pasted id is taken as it is. Anything else is looked up on TVMaze, whose
    search returns the IMDb id in the show's externals; EZTV has no title
    search, and its HTML pages are behind Cloudflare on every mirror.
    """
    pasted = _IMDB_RE.match((q or "").strip())
    if pasted:
        return pasted.group(1), (q or "").strip()
    try:
        found = get_json(_TVMAZE_SEARCH, params={"q": q})
    except Exception as exc:
        raise ApiError(
            "Could not look up that show right now. Try again, or paste its "
            "IMDb id (tt...). (%s)" % exc,
            ApiError._SERVICE_UNAVAILABLE,
        )
    for entry in found or []:
        show = entry.get("show") or {}
        imdb = (show.get("externals") or {}).get("imdb") or ""
        if imdb[:2] == "tt" and len(imdb) > 2:
            return imdb[2:], show.get("name") or str(q)
    raise ApiError(
        "No show found for '%s'. Try a different title, or paste its IMDb id "
        "(tt...)." % q,
        404,
    )


def _episodes(imdb):
    """
    Every episode EZTV has for the show, newest first, up to a page cap.

    The API pages at a hundred; the first answer says how many there are, so
    the remaining pages are read until the cap rather than guessed at.
    """
    data = get_json(_BASE_API, params={"imdb_id": imdb, "limit": 100, "page": 1})
    torrents = list(data.get("torrents") or [])
    try:
        total = int(data.get("torrents_count") or len(torrents))
    except (TypeError, ValueError):
        total = len(torrents)
    pages = min(_MAX_PAGES, max(1, int(math.ceil(total / 100.0))))
    for page in range(2, pages + 1):
        more = get_json(
            _BASE_API, params={"imdb_id": imdb, "limit": 100, "page": page}
        )
        batch = more.get("torrents") or []
        if not batch:
            break
        torrents.extend(batch)
    return torrents


def _episode_order(t):
    def number(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    try:
        seeds = int(t.get("seeds") or 0)
    except (TypeError, ValueError):
        seeds = 0
    return (number(t.get("season")), number(t.get("episode")), seeds)


def search(q):
    """
    A show's episodes, by title or by a pasted IMDb id.

    The title is resolved to an id with TVMaze, and EZTV is asked by that id.
    Newest episode first, and the rows carry the id so Add Subtitles can use it.
    """
    imdb, show = _imdb_for(q)
    try:
        torrents = _episodes(imdb)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            "EZTV did not answer for '%s'. Try again. (%s)" % (show, exc),
            ApiError._SERVICE_UNAVAILABLE,
        )
    torrents = [t for t in torrents if t.get("magnet_url")]
    if not torrents:
        raise ApiError(
            "EZTV has no episodes of '%s'. It may not carry the show, or the id "
            "may be wrong." % show,
            404,
        )
    torrents.sort(key=_episode_order, reverse=True)
    return _rows(torrents, imdb=imdb)

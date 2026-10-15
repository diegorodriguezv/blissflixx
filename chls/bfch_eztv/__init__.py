from datetime import datetime, timezone

from lib.api.torrent import TorrentPlayItem
from lib.chanutils import get_json
from lib.playitem import PlayItemList


_BASE_API = "https://eztvx.to/api/get-torrents"

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


def feed(idx):
    data = get_json(_BASE_API, params={"limit": 100, "page": idx + 1})
    results = PlayItemList()
    for t in data.get("torrents", []) or []:
        magnet = t.get("magnet_url")
        if not magnet:
            continue
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
        item = TorrentPlayItem(
            title, "/img/icons/film.svg", magnet, subtitle, subs=None
        )
        results.add(item)
    return results


# No search(). The API cannot do one.
#
# This is the JSON endpoint that replaced EZTV's HTML pages, and it answers by
# imdb_id -- /api/get-torrents?imdb_id=1190634 -- not by title. It has no
# title parameter and no show list to search: the HTML search and show pages that
# did are behind a 403 from Cloudflare on every mirror, which is why the listing
# moved to this API in the first place.
#
# So a search box here would search nothing. It used to return an empty list,
# which is the same failure wearing a nicer mask: the interface said "There are no
# results for this query", which reads as "this film is not on EZTV" when the
# truth is "this box was never going to look". Leaving the function out means the
# box is not drawn, and what is left -- the two listings -- does work.
#
# If a title search is wanted later it has to be built: the show has to be
# resolved to an imdb_id first, which means a source that maps titles to ids.
# That is a feature, not a repair, and it is not here.

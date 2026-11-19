"""
TheRarBg's torrents, from the RSS search endpoint.

The site has an HTML listing too, but the RSS is the whole row -- title,
magnet, size, seeders, date and category -- in one answer. The HTML puts the
magnet on a detail page, which would be one request per torrent. The RSS sorts
newest first, so the latest feeds are the recent releases and search is
filtered to the categories this player can actually open.

RARBG closed in 2023. "The RarBg" is a successor using the name, not the old
site's catalogue; treat its provenance accordingly.
"""

import re
import xml.etree.ElementTree as ET
from datetime import timezone
from email.utils import parsedate_to_datetime

from lib.api.torrent import TorrentPlayItem
from lib.chanutils import get, movie_title_year, series_season_episode
from lib.playitem import PlayItemList

_BASE = "https://therarbg.com"
_RSS = _BASE + "/api/rss/search/"

#: Only these can be played here. Books, Games and Applications are in the
#: index and in an unfiltered search, but a row that opens no media is noise.
_PLAYABLE = ("Movies", "TV", "Anime", "Music")

_FEEDLIST = [
    {"title": "Latest Movies", "category": "Movies"},
    {"title": "Latest TV Shows", "category": "TV"},
    {"title": "Latest Anime", "category": "Anime"},
    {"title": "Latest Music", "category": "Music"},
]

_FILM_ICON = "/img/icons/film.svg"
_MUSIC_ICON = "/img/icons/music.svg"

#: "240.3 MB - 10 seeders - quality 31/100"
_SIZE_SEEDS = re.compile(r"^(?P<size>[^-]+?)\s+-\s+(?P<seeds>\d+)\s+seeders")


def name():
    return "The RarBg"


def image():
    return "icon.png"


def description():
    return "The RarBg Channel (<a target='_blank' href='https://therarbg.com'>https://therarbg.com</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    entry = _FEEDLIST[idx]
    return _fetch("", entry["category"])


def search(q):
    return _fetch(q, None)


def _fetch(query, category):
    params = {"q": query}
    if category:
        params["category"] = category
    response = get(_RSS, params=params)
    return _extract(response.content, category)


def _extract(content, category=None):
    root = ET.fromstring(content)
    results = []
    for item in root.findall("./channel/item"):
        row_category = (item.findtext("category") or "").strip()
        if category and row_category != category:
            continue
        if row_category not in _PLAYABLE:
            continue
        enclosure = item.find("enclosure")
        magnet = enclosure.get("url") if enclosure is not None else None
        if not magnet:
            continue
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        size, seeds = _size_and_seeds(item.findtext("description") or "")
        released = _date(item.findtext("pubDate"))
        img, subs = _describe(row_category, title)
        subtitle = "Size: %s, Seeds: %d, Released: %s" % (size, seeds, released)
        results.append((seeds, title, img, magnet, subtitle, subs))

    results.sort(key=lambda result: result[0], reverse=True)
    pil = PlayItemList()
    for _, title, img, magnet, sub, subs in results:
        pil.add(TorrentPlayItem(title, img, magnet, sub, subs=subs))
    return pil


def _size_and_seeds(description):
    found = _SIZE_SEEDS.match(description)
    if found is None:
        return "NA", 0
    try:
        seeds = int(found.group("seeds"))
    except ValueError:
        seeds = 0
    return found.group("size").strip(), seeds


def _date(text):
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "NA"


def _describe(category, title):
    if category == "Music":
        return _MUSIC_ICON, None
    if category == "Movies":
        return _FILM_ICON, movie_title_year(title)
    if category == "TV":
        return _FILM_ICON, series_season_episode(title)
    if category == "Anime":
        episode = series_season_episode(title)
        return _FILM_ICON, episode if "episode" in episode else None
    return _FILM_ICON, None

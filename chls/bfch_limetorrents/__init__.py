import re
import urllib.parse

from lib.api.torrent import TorrentPlayItem
from lib.chanutils import (
    get_attr,
    get_doc,
    get_text,
    movie_title_year,
    select_all,
    select_one,
    series_season_episode,
)
from lib.chanutils.torrent import subtitle
from lib.playitem import PlayItemList

_BASE = "https://www.limetorrents.fun"
_SEARCH_URL = _BASE + "/search/all/%s/seeds/1/"

#: The category a feed belongs to. Browsing pages do not repeat it on every
#: row -- only search results do, as "1 Year+ - in Movies" -- so the feed has
#: to remember what it asked for.
_FEEDLIST = [
    {
        "title": "Latest Movies",
        "category": "Movies",
        "url": _BASE + "/browse-torrents/Movies/",
    },
    {
        "title": "Popular Movies",
        "category": "Movies",
        "url": _BASE + "/browse-torrents/Movies/seeds/1/",
    },
    {
        "title": "Latest TV Shows",
        "category": "TV-shows",
        "url": _BASE + "/browse-torrents/TV-shows/",
    },
    {
        "title": "Popular TV Shows",
        "category": "TV-shows",
        "url": _BASE + "/browse-torrents/TV-shows/seeds/1/",
    },
    {
        "title": "Latest Music",
        "category": "Music",
        "url": _BASE + "/browse-torrents/Music/",
    },
    {
        "title": "Latest Anime",
        "category": "Anime",
        "url": _BASE + "/browse-torrents/Anime/",
    },
]

_FILM_ICON = "/img/icons/film.svg"
_MUSIC_ICON = "/img/icons/music.svg"

#: The listing writes the category into the "added" column on search pages:
#: "1 Year+ - in Movies". Browse pages leave it out.
_IN_CATEGORY = re.compile(r"- in ([A-Za-z -]+)$")


def name():
    return "LimeTorrents"


def image():
    return "icon.png"


def description():
    return "LimeTorrents Channel (<a target='_blank' href='https://www.limetorrents.fun'>https://www.limetorrents.fun</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    entry = _FEEDLIST[idx]
    return _extract(get_doc(entry["url"]), entry.get("category"))


def search(q):
    url = _SEARCH_URL % urllib.parse.quote(q.encode("utf8"))
    return _extract(get_doc(url))


def _extract(doc, category=None):
    """
    The rows of a LimeTorrents listing.

    The download anchor points at itorrents.net, a host that no longer answers
    often enough to rely on. It is not fetched: the URL contains the torrent's
    SHA-1, and playing it runs torrent2magnet() on the URL, which turns the hash
    into a magnet and adds our trackers. The dead host is only a carrier for
    the hash.
    """
    results = []
    for row in select_all(doc, "table.table2 tr"):
        torrent_el = select_one(row, "div.tt-name a[href*='.torrent']")
        if torrent_el is None:
            continue
        url = get_attr(torrent_el, "href")
        anchors = select_all(row, "div.tt-name a")
        if not anchors or not url:
            continue
        title = get_text(anchors[-1])
        if not title or ".torrent" in title.lower():
            # Some rows carry only the download icon; the title anchor is the
            # other one. A download link's text is not a title.
            title = get_text(anchors[0])
        if not title:
            continue

        normal = select_all(row, "td.tdnormal")
        size = get_text(normal[-1]) if normal else "NA"
        added = get_text(normal[0]) if normal else None
        row_category = category
        if added:
            found = _IN_CATEGORY.search(added)
            if found:
                row_category = found.group(1).strip()

        seeds = _number(select_one(row, "td.tdseed"))
        peers = _number(select_one(row, "td.tdleech"))
        img, subs = _describe(row_category, title)
        results.append((seeds, title, img, url, subtitle(size, seeds, peers), subs))

    results.sort(key=lambda result: result[0], reverse=True)
    pil = PlayItemList()
    for seeds, title, img, url, sub, subs in results:
        pil.add(TorrentPlayItem(title, img, url, sub, subs=subs))
    return pil


def _number(el):
    text = get_text(el)
    if not text:
        return 0
    try:
        return int(text.replace(",", "").strip())
    except ValueError:
        return 0


def _describe(category, title):
    """
    The icon for the row and what to ask OpenSubtitles for, if anything.

    Only a film or an episode has a title a subtitle search can use. Music
    rows, and anime rows that name an episode without an SxxExx marker, get
    no dictionary: passing the release name as a title finds nothing.
    """
    if category == "Music":
        return _MUSIC_ICON, None
    if category == "Anime":
        episode = series_season_episode(title)
        return _FILM_ICON, episode if "episode" in episode else None
    if category == "TV-shows":
        return _FILM_ICON, series_season_episode(title)
    if category == "Movies":
        return _FILM_ICON, movie_title_year(title)
    # Search results, where the category column is the only hint.
    episode = series_season_episode(title)
    if "episode" in episode:
        return _FILM_ICON, episode
    movie = movie_title_year(title)
    if "year" in movie:
        return _FILM_ICON, movie
    return _FILM_ICON, None

"""
EliteTorrent, the long-running Spanish torrent index.

The site has WordPress RSS feeds for its sections, which give the title, link,
date and poster in one request. The playable link is not in the feed: the
detail page holds it in a shortener URL whose `i` parameter is the URL
base64-encoded several times and then ROT13'd. Both the .torrent and the magnet
links carry the same encoding, so the shortener is never visited -- decode the
parameter and take the magnet when there is one.

One detail page per row is the cost; the feed cap and a small worker pool keep
it to a few seconds.
"""

import base64
import codecs
import re
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import timezone
from email.utils import parsedate_to_datetime

from lib.api.torrent import TorrentPlayItem
from lib.chanutils import (
    get,
    get_attr,
    get_doc,
    get_text,
    movie_title_year,
    select_all,
    series_season_episode,
)
from lib.playitem import PlayItemList

_BASE = "https://www.elitetorrent.com"

#: The section feeds that actually have items. microHD, 720p and DVDRip
#: answer with an empty WordPress feed, so they are not offered.
_FEEDLIST = [
    {"title": "Películas", "url": _BASE + "/peliculas/feed/"},
    {"title": "Series", "url": _BASE + "/series/feed/"},
    {"title": "1080p", "url": _BASE + "/calidad/1080p-10-1/feed/"},
    {"title": "4K", "url": _BASE + "/calidad/4k/feed/"},
]

_MEDIA = "{http://search.yahoo.com/mrss/}content"
_MAX_ITEMS = 12
_MAX_RESULTS = 20
_WORKERS = 4

_FILM_ICON = "/img/icons/film.svg"
_B64_RE = re.compile(r"^[A-Za-z0-9+/=]+$")
_I_RE = re.compile(r"[?&]i=([^&]+)")
_EPISODE_RE = re.compile(r"^(.*?)\s*(\d{1,2})\s*[x×]\s*(\d{1,2})$")
_T_SEASON_RE = re.compile(r"^(.*?)\s*[Tt](\d{1,2})\s*[Ee](\d{1,2})\b")


def name():
    return "EliteTorrent"


def image():
    return "icon.png"


def description():
    return "EliteTorrent Channel, Español (<a target='_blank' href='https://www.elitetorrent.com'>https://www.elitetorrent.com</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    entry = _FEEDLIST[idx]
    return _from_items(_rss(entry["url"]), entry["title"], _MAX_ITEMS)


def search(q):
    response = get(_BASE + "/", params={"s": q, "feed": "rss2"})
    return _from_items(_parse_rss(response.content), None, _MAX_RESULTS)


def _rss(url):
    return _parse_rss(get(url).content)


def _parse_rss(content):
    root = ET.fromstring(content)
    items = []
    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        poster_el = item.find(_MEDIA)
        poster = poster_el.get("url") if poster_el is not None else None
        items.append(
            {
                "title": title,
                "link": link,
                "poster": poster,
                "released": _date(item.findtext("pubDate")),
            }
        )
    return items


def _from_items(items, quality, limit):
    items = items[:limit]
    if not items:
        return PlayItemList()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        urls = list(pool.map(_safe_play_url, [item["link"] for item in items]))

    results = PlayItemList()
    for item, url in zip(items, urls):
        if not url:
            continue
        bits = []
        if quality in ("1080p", "4K"):
            bits.append("Calidad: " + quality)
        if item["released"] != "NA":
            bits.append("Released: " + item["released"])
        subtitle = ", ".join(bits) or None
        results.add(
            TorrentPlayItem(
                item["title"],
                item["poster"] or _FILM_ICON,
                url,
                subtitle,
                subs=_subs(item["title"]),
            )
        )
    return results


def _safe_play_url(url):
    try:
        return _play_url(url)
    except Exception:
        return None


def _play_url(url):
    doc = get_doc(url)
    decoded = []
    for link in select_all(doc, "a.enlace_torrent"):
        value = _decode_link(get_attr(link, "href") or "")
        if value:
            decoded.append(value)
    for value in decoded:
        if value.startswith("magnet:"):
            return value
    for value in decoded:
        if value.startswith(("http://", "https://")) and ".torrent" in value:
            return value
    return None


def _decode_link(href):
    """The URL behind the shortener's `i` parameter, without the shortener."""
    found = _I_RE.search(href)
    if found is None:
        return None
    value = urllib.parse.unquote(found.group(1))
    for _ in range(8):
        if value.startswith(("magnet:", "http://", "https://")):
            return value
        try:
            value = base64.b64decode(value, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            break
        if not _B64_RE.match(value):
            break
    value = codecs.decode(value, "rot_13")
    if value.startswith(("magnet:", "http://", "https://")):
        return value
    return None


def _date(text):
    try:
        return parsedate_to_datetime(text).astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError):
        return "NA"


def _subs(title):
    found = _EPISODE_RE.match(title)
    if found:
        series = found.group(1).strip()
        return {
            "series": series,
            "season": int(found.group(2)),
            "episode": int(found.group(3)),
        }
    found = _T_SEASON_RE.match(title)
    if found:
        series = found.group(1).strip()
        return {
            "series": series,
            "season": int(found.group(2)),
            "episode": int(found.group(3)),
        }
    parsed = series_season_episode(title)
    if "episode" in parsed:
        return parsed
    return movie_title_year(title)

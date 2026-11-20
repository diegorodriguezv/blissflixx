"""
DivxTotal, the Spanish torrent index with Castellano and Latino audio.

The listing is a table of links; the playable link is on the detail page, in a
base64 attribute rather than an href. The site wraps it in a shortener too,
but the base64 is self-contained -- both the "Opcion 1" data-src and the
"Opcion 2" download_tt.php?u= value decode to the same .torrent -- so the
shortener is never visited.

One request per row is the cost of that design: a feed reads the table, then
opens each detail page. The feed cap and the small worker pool keep it to a
few seconds instead of a minute.
"""

import base64
import re
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

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
from lib.playitem import PlayItemList

_BASE = "https://divxtotal.foo"

#: A feed opens this many detail pages. Search allows a few more because a
#: query is a deliberate request rather than a page that loads on its own.
_MAX_ITEMS = 12
_MAX_RESULTS = 20
_WORKERS = 4

_FEEDLIST = [
    {"title": "Películas", "url": _BASE + "/peliculas/"},
    {"title": "Películas HD", "url": _BASE + "/peliculas-hd/"},
    {"title": "Series", "url": _BASE + "/series/"},
    {"title": "Audio Latino", "url": _BASE + "/?s=audio+latino"},
    {"title": "Películas 4K", "url": _BASE + "/peliculas-4k/"},
    {"title": "Películas DVDR", "url": _BASE + "/peliculas-dvdr/"},
]

_FILM_ICON = "/img/icons/film.svg"
_EPISODE_RE = re.compile(r"^(.*?)(\d{1,2})\s*[x×]\s*(\d{1,2})$", re.IGNORECASE)
_DOWNLOAD_RE = re.compile(r"download_tt\.php\?u=([^&]+)")

#: The flag files the download table uses. ES is the common one; the title
#: usually says "[Audio Latino]" when that is what the release carries.
_FLAGS = {
    "ES": "Español",
    "LA": "Latino",
    "VO": "VOSE",
    "VOSE": "VOSE",
    "EN": "Inglés",
}


def name():
    return "DivxTotal (Latino)"


def image():
    return "icon.png"


def description():
    return "DivxTotal Channel, Castellano y Latino (<a target='_blank' href='https://divxtotal.foo'>https://divxtotal.foo</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return _from_entries(_listing(_FEEDLIST[idx]["url"], _MAX_ITEMS), _MAX_ITEMS)


def search(q):
    url = _BASE + "/?s=" + urllib.parse.quote(q.encode("utf8"))
    return _from_entries(_listing(url, _MAX_RESULTS), _MAX_RESULTS)


def _listing(url, limit):
    """The detail-page links from one listing or search-results table."""
    doc = get_doc(url)
    links = []
    for row in select_all(doc, "table.table tr"):
        link = select_one(row, "td a[href]")
        if link is not None:
            links.append(link)
    if not links:
        # The series index is cards, not a table.
        links = select_all(doc, "a[rel='bookmark']")
    if not links:
        links = select_all(doc, "p.seccontnom a[href]")

    entries = []
    seen = set()
    for link in links:
        href = get_attr(link, "href") or ""
        absolute = urllib.parse.urljoin(_BASE, href)
        path = urllib.parse.urlparse(absolute).path
        if not (path.startswith("/peliculas") or path.startswith("/series")):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        entries.append(absolute)
        if len(entries) >= limit:
            break
    return entries


def _from_entries(entries, limit):
    if not entries:
        return PlayItemList()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        details = list(pool.map(_safe_detail, entries))

    # A movie is one row; a series page is a whole season or show. Take one
    # row from each page in turn so a series feed is a spread of shows rather
    # than the first show's back catalogue.
    pages = [rows for rows in details if rows]
    results = PlayItemList()
    row = 0
    while len(results.itemlist) < limit and any(len(rows) > row for rows in pages):
        for rows in pages:
            if row < len(rows):
                title, img, url, subtitle, subs = rows[row]
                results.add(TorrentPlayItem(title, img, url, subtitle, subs=subs))
                if len(results.itemlist) >= limit:
                    break
        row += 1
    return results


def _safe_detail(url):
    """One detail page, or nothing. A broken row must not fail the feed."""
    try:
        return _detail(url)
    except Exception:
        return []


def _detail(url):
    doc = get_doc(url)
    show = (get_text(select_one(doc, "h1")) or "").strip()
    poster = select_one(doc, "#bloque_poster img")
    img = None
    if poster is not None:
        img = get_attr(poster, "src") or get_attr(poster, "data-pagespeed-lazy-src")
    info = _info(doc)
    is_series = urllib.parse.urlparse(url).path.startswith("/series/")

    rows = select_all(doc, "table.rwd-table tr")
    if not rows:
        rows = select_all(doc, "tr")

    found = []
    seen = set()
    for row in rows:
        link = select_one(row, "a.linktorrent[data-src]")
        if link is None:
            link = select_one(row, "a.linktorrent[href*='download_tt']")
        if link is None:
            link = select_one(row, "a[href$='.torrent']")
        if link is None:
            continue
        torrent = _torrent_url(link)
        if not torrent or torrent in seen:
            continue
        seen.add(torrent)

        cells = [(get_text(cell) or "").strip() for cell in select_all(row, "td")]
        flag = _flag(row)
        if is_series:
            label = (get_text(link) or "").strip()
            title, subs = _episode(show, label)
            subtitle = _subtitle(flag or info.get("Idioma"), "", "", "")
        else:
            quality = cells[2] if len(cells) > 2 else ""
            fmt = cells[1] if len(cells) > 1 else ""
            size = cells[3] if len(cells) > 3 else ""
            title = show
            if quality and quality.upper() != "N/A":
                title = "%s [%s]" % (show, quality)
            subs = movie_title_year(show) if show else None
            subtitle = _subtitle(
                info.get("Idioma") or flag,
                quality,
                info.get("Formato") or fmt,
                size,
            )
        found.append((title, img, torrent, subtitle, subs))
    return found


def _episode(show, label):
    """A series row's label, e.g. "Reacher4x5", as a title and subtitle key."""
    found = _EPISODE_RE.match(label)
    if found:
        series = (found.group(1).strip() or show).strip()
        season = int(found.group(2))
        episode = int(found.group(3))
        title = "%s S%02dE%02d" % (series, season, episode)
        return title, {"series": series, "season": season, "episode": episode}
    parsed = series_season_episode(label)
    if "episode" in parsed:
        series = parsed.get("series") or show
        season = parsed["season"]
        episode = parsed["episode"]
        return (
            "%s S%02dE%02d" % (series, season, episode),
            {"series": series, "season": season, "episode": episode},
        )
    title = ("%s %s" % (show, label)).strip()
    return title, ({"series": show} if show else None)


def _torrent_url(link):
    """The .torrent behind the base64, without visiting the shortener."""
    data = get_attr(link, "data-src")
    if not data:
        href = get_attr(link, "href") or ""
        found = _DOWNLOAD_RE.search(href)
        if found:
            data = urllib.parse.unquote(found.group(1))
    if not data:
        return None
    try:
        url = base64.b64decode(urllib.parse.unquote(data)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not url.startswith("http") or ".torrent" not in url:
        return None
    # Some episode files use "×" in the name. Quote it here so the item's URL
    # is a valid ASCII URI before it reaches the player or the fetcher.
    return urllib.parse.quote(url, safe=":/?&=%#[]@!$'()*+,;~-._")


def _info(doc):
    """The "Idioma: Español" style blocks on a detail page."""
    info = {}
    for block in select_all(doc, "div.info-item"):
        parts = select_all(block, "p")
        if len(parts) < 2:
            continue
        key = (get_text(parts[0]) or "").rstrip(":").strip()
        value = (get_text(parts[-1]) or "").strip()
        if key and value:
            info[key] = value
    return info


def _flag(row):
    img = select_one(row, "td img")
    if img is None:
        return None
    src = get_attr(img, "data-pagespeed-lazy-src") or get_attr(img, "src") or ""
    name = src.rsplit("/", 1)[-1].split(".")[0].upper()
    return _FLAGS.get(name)


def _subtitle(language, quality, fmt, size):
    bits = []
    if language and language.upper() != "N/A":
        bits.append("Idioma: " + language)
    if quality and quality.upper() != "N/A":
        bits.append("Calidad: " + quality)
    if fmt and fmt.upper() != "N/A":
        bits.append("Formato: " + fmt)
    if size and size.upper() != "N/A":
        bits.append("Peso: " + size)
    return ", ".join(bits) or None

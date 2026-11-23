import json
import urllib.parse

import lxml.html

from lib.chanutils import get, get_attr, get_text, select_all, select_one
from lib.playitem import PlayItem, PlayItemList

#: Everything this channel plays is a trailer, and no subtitle service has
#: trailers. Offering Add Subtitles on a trailer searched OpenSubtitles for
#: the film's name and found the film's subtitles -- subtitles that do not
#: match the trailer's cut, if the film has any at all. The platform reads
#: this and does not draw the button.
SUPPORTS_SUBTITLES = False

_BASE = "https://www.rottentomatoes.com"

_SEARCH_URL = _BASE + "/search"

_FEEDLIST = [
    {
        "title": "Top Rentals",
        "url": _BASE + "/browse/movies_at_home/sort:popular",
    },
    {
        "title": "Top Rated",
        "url": _BASE + "/browse/movies_at_home/critics:certified_fresh",
    },
    {
        "title": "New Releases",
        "url": _BASE + "/browse/movies_at_home/sort:newest",
    },
]


def name():
    return "Rotten Tomatoes"


def image():
    return "icon.png"


def description():
    return "Rotten Tomatoes Channel (<a target='_blank' href='https://www.rottentomatoes.com/'>https://www.rottentomatoes.com/</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    r = get(_FEEDLIST[idx]["url"])
    return _extract(r.text)


def search(q):
    """The site's own search; see _search_rows for what the answers carry."""
    r = get(_SEARCH_URL, params={"search": q})
    return _search_rows(r.text)


def _search_rows(text):
    """
    The search page's server-rendered rows: each carries its tomatometer,
    its year and its cast, so the results say more about a film than a bare
    list of titles would.
    """
    doc = lxml.html.fromstring(text)
    results = PlayItemList()
    for row in select_all(doc, "search-page-media-row"):
        title_el = select_one(row, "a[slot='title']")
        href = (get_attr(title_el, "href") or "").strip()
        # The search also answers people and shows; this channel is films.
        if not href or "/m/" not in href:
            continue
        title = (get_text(title_el) or "").strip()
        if not title:
            title = (get_attr(select_one(row, "img"), "alt") or "").strip()
        if not title:
            continue
        url = href if href.startswith("http") else _BASE + href
        img = get_attr(select_one(row, "img"), "src")
        year = (get_attr(row, "release-year") or "").strip()
        cast = (get_attr(row, "cast") or "").strip().replace(",", ", ")
        subtitle = " · ".join(part for part in (year, cast) if part)
        certified = (get_attr(row, "tomatometer-is-certified") or "").lower() == "true"
        tomatometer = (get_attr(row, "tomatometer-score") or "").strip()
        synopsis = _rating_line(url, tomatometer=tomatometer, certified=certified)
        if not synopsis:
            # A film too new or too obscure for a score still gets its page:
            # the link is the one thing every row here can offer.
            synopsis = '<a target="_blank" href="%s">Rotten Tomatoes page</a>' % url
        results.add(PlayItem(title, img, url, subtitle, synopsis))
    return results


def _rating_line(url, tomatometer=None, popcornmeter=None, certified=False):
    """
    The scores as the site itself names them, each one a link to the film's
    page: a number on its own said nothing, and the only way to see what a
    "16" was made of was the page the link goes to.
    """
    parts = []
    if tomatometer:
        label = "Tomatometer"
        if certified:
            label += " (Certified Fresh)"
        parts.append(
            '<a target="_blank" href="%s">%s: %s%%</a>' % (url, label, tomatometer)
        )
    if popcornmeter:
        parts.append('<a target="_blank" href="%s">Popcornmeter: %s%%</a>' % (url, popcornmeter))
    return " · ".join(parts)


def _movie_url(path):
    path = (path or "").strip()
    if not path:
        return ""
    return path if path.startswith("http") else _BASE + path


def _extract(text):
    """
    The browse page, which is server-rendered tiles: each carries the film's
    poster, its streaming date, and both of the site's scores. Falling back
    to the structured item list the page also publishes, for the day the
    tiles change shape again.
    """
    results = _extract_tiles(text)
    if len(results.to_list()) > 0:
        return results
    return _extract_ldjson(text)


def _extract_tiles(text):
    results = PlayItemList()
    doc = lxml.html.fromstring(text)
    for tile in select_all(doc, "media-info-tile"):
        poster = select_one(tile, "poster-tile")
        url = _movie_url(get_attr(poster, "media-url"))
        if not url:
            url = _movie_url(get_attr(select_one(tile, "a.js-tile-link"), "href"))
        if not url:
            continue
        img = get_attr(select_one(poster, "rt-img"), "src")
        title = (
            get_text(select_one(tile, "rt-text[data-qa='discovery-media-list-item-title']"))
            or get_attr(select_one(poster, "rt-img"), "alt")
            or ""
        ).strip()
        if not title:
            continue
        tomatometer = (get_text(select_one(tile, "rt-text[slot='criticsScore']")) or "").strip().rstrip("%")
        popcornmeter = (get_text(select_one(tile, "rt-text[slot='audienceScore']")) or "").strip().rstrip("%")
        certified = (get_attr(select_one(tile, "score-icon-critics"), "certified") or "").lower() == "true"
        streaming = (
            get_text(select_one(tile, "rt-text[data-qa='discovery-media-list-item-start-date']")) or ""
        ).strip()
        results.add(
            PlayItem(
                title,
                img,
                url,
                streaming,
                _rating_line(
                    url,
                    tomatometer=tomatometer,
                    popcornmeter=popcornmeter,
                    certified=certified,
                ),
            )
        )
    return results


def _movies_from_data(data):
    movies = []
    item_list = data.get("itemListElement") if isinstance(data, dict) else None
    if isinstance(item_list, dict):
        nested = item_list.get("itemListElement")
        if isinstance(nested, list):
            movies = nested
        elif isinstance(nested, dict):
            movies = [nested]
    elif isinstance(item_list, list):
        movies = item_list

    if not movies:
        # Fall back to a recursive search for movie entries.
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if current.get("@type") == "Movie":
                    movies.append(current)
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
    return movies


def _extract_ldjson(text):
    results = PlayItemList()
    doc = lxml.html.fromstring(text)
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content())
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for item in _movies_from_data(data):
            if not isinstance(item, dict) or item.get("@type") != "Movie":
                continue
            name = item.get("name")
            image = item.get("image")
            url = _movie_url(item.get("url"))
            subtitle = item.get("dateCreated")
            if not name or not url:
                continue
            tomatometer = None
            aggregate_rating = item.get("aggregateRating")
            if isinstance(aggregate_rating, dict):
                tomatometer = str(aggregate_rating.get("ratingValue") or "").strip()
            results.add(
                PlayItem(
                    name, image, url, subtitle, _rating_line(url, tomatometer=tomatometer)
                )
            )
    return results

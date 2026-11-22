"""
EZTV's listings, its trending shows, and a show-first search.

EZTV's JSON API has two useful answers: the latest torrents
(/api/get-torrents?limit=&page=) and one show's episodes by IMDb id
(?imdb_id=1190634). It has no title search, no sorting, no genres and no
countries; the HTML pages that used to offer them are behind Cloudflare on
every mirror, so there is nothing to scrape there.

So the channel is built the way EZTV is used: a show, then its episodes.

- Latest is EZTV's own feed.
- Trending is built from the latest few pages: the shows whose recent releases
  are being seeded most, which is EZTV's own audience rather than a borrowed
  popularity list. Their posters and their own names come from TVMaze, looked
  up by IMDb id and kept for a day; a show TVMaze does not know keeps the
  release name and an icon.
- Genres, types and countries come from TVMaze's show browser, which can be
  read without a key. Picking one opens that show's EZTV episodes by IMDb id.
- A title search resolves with TVMaze. One match opens its episodes; several
  matches are a picker, because "harry potter" is a not-yet-aired series and a
  baking show, and guessing between them is how a search finds nothing.

Show rows carry showmore:// links; the show page lists the episodes most
seeded first, every row wearing the show's poster. Episode rows carry the IMDb
id, so Add Subtitles asks OpenSubtitles by id instead of by the release name.
"""

import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from lib.api.common import ApiError
from lib.api.torrent import TorrentPlayItem, icon_for_title
from lib.chanutils import (
    get_attr,
    get_doc,
    get_json,
    get_text,
    get_text_content,
    number_commas,
    select_all,
    select_one,
    title_query,
)
from lib.playitem import PlayItemList, ShowMoreItem

_BASE_API = "https://eztvx.to/api/get-torrents"

#: TVMaze maps a title to an IMDb id. EZTV's API answers by id only, so this is
#: the half of a title search that EZTV cannot do.
_TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows"
_TVMAZE_SHOW = "https://api.tvmaze.com/shows/%s"

#: TVMaze's show browser: the API has no sorting, but the site does. These are
#: the filter values its form posts (ids, not labels), read from the form.
_TVMAZE_BROWSE = "https://www.tvmaze.com/shows"

#: TVMaze's lookup by IMDb id, for a show's poster and proper name. EZTV
#: carries neither: every screenshot field in its API is empty.
_TVMAZE_LOOKUP = "https://api.tvmaze.com/lookup/shows"

#: A pasted id, with or without the tt. EZTV wants the digits.
_IMDB_RE = re.compile(r"^(?:tt)?(\d{3,})$", re.IGNORECASE)

#: How many pages of one show's episodes are read. The API pages at 100, so
#: this is the newest 500 releases; long-running soaps have more, and the
#: oldest seasons are not worth five hundred rows of scrolling to reach.
_MAX_PAGES = 5

#: How much of the latest feed the trending list samples, and how many shows
#: it keeps. Five pages is a few days of releases; the top thirty is a screen.
_TRENDING_PAGES = 5
_TRENDING_SHOWS = 30

#: How many pages of 25 shows a browse feed reads.
_SHOW_PAGES = 2

#: How many requests are open at once while a feed is being built. Trending
#: reads five EZTV pages and up to thirty TVMaze lookups; one at a time, the
#: lookups alone took twenty-five seconds on a first build, which is not a
#: wait to put in front of a television.
_WORKERS = 8

#: How many shows one latest build may look up for the first time. A page of
#: releases names about thirty shows, most already known from an earlier
#: build; the cap is for a page that names many more, so the feed is never a
#: long wait and the rest get their poster from the cache next time.
_LATEST_FRESH = 24

#: A browse page changes slowly; recent seed counts move faster. A show's
#: poster and name barely change at all.
_CACHE_TTL = 60 * 60
_TRENDING_TTL = 10 * 60
_SHOW_TTL = 24 * 60 * 60
_CACHE = {}

#: The feeds, in the order the page shows them. A name is stable and goes in
#: the link, so a reordered list cannot open the wrong genre or country.
_FEEDLIST = [
    {"title": "Latest", "name": "latest", "source": "latest"},
    {
        "title": "Trending",
        "name": "trending",
        "group": "Shows",
        "source": "trending",
    },
    {
        "title": "Drama",
        "name": "genre-drama",
        "group": "Genres",
        "browse": {"Show[genre]": "1"},
    },
    {
        "title": "Comedy",
        "name": "genre-comedy",
        "group": "Genres",
        "browse": {"Show[genre]": "2"},
    },
    {
        "title": "Science-Fiction",
        "name": "genre-scifi",
        "group": "Genres",
        "browse": {"Show[genre]": "14"},
    },
    {
        "title": "Anime",
        "name": "genre-anime",
        "group": "Genres",
        "browse": {"Show[genre]": "5"},
    },
    {
        "title": "Crime",
        "name": "genre-crime",
        "group": "Genres",
        "browse": {"Show[genre]": "8"},
    },
    {
        "title": "Reality",
        "name": "type-reality",
        "group": "Types",
        "browse": {"Show[showType_enum]": "3"},
    },
    {
        "title": "Documentary",
        "name": "type-documentary",
        "group": "Types",
        "browse": {"Show[showType_enum]": "5"},
    },
    {
        "title": "United States",
        "name": "country-us",
        "group": "Countries",
        "browse": {"Show[country_enum]": "235"},
    },
    {
        "title": "United Kingdom",
        "name": "country-uk",
        "group": "Countries",
        "browse": {"Show[country_enum]": "234"},
    },
    {
        "title": "Canada",
        "name": "country-ca",
        "group": "Countries",
        "browse": {"Show[country_enum]": "40"},
    },
    {
        "title": "Australia",
        "name": "country-au",
        "group": "Countries",
        "browse": {"Show[country_enum]": "14"},
    },
]


def name():
    return "EZTV"


def image():
    return "icon.png"


def description():
    return "EZTV Torrents Channel (<a target='_blank' href='https://eztvx.to'>https://eztvx.to</a>)."


def feedlist():
    return _FEEDLIST


def _cached(key, ttl):
    entry = _CACHE.get(key)
    if entry and time.monotonic() - entry[0] < ttl:
        return entry[1]
    return None


def _cache(key, value):
    _CACHE[key] = (time.monotonic(), value)


def _feed_index(idx, name):
    """
    The feed a route names, by its stable name first.

    The list is edited by hand, so an index in a link can point at another
    genre after a change. The name does not move. A link without one, or with
    a stale one, still opens its index.
    """
    if name:
        name = str(name)
        for i, feed in enumerate(_FEEDLIST):
            if feed.get("name") == name:
                return i
        lowered = name.lower()
        for i, feed in enumerate(_FEEDLIST):
            if (feed.get("title") or "").lower() == lowered:
                return i
    try:
        i = int(idx)
    except (TypeError, ValueError):
        i = 0
    return i if 0 <= i < len(_FEEDLIST) else 0


def feed(idx):
    return _feed_for(_FEEDLIST[_feed_index(idx, None)])


def feed_by_name(idx, name):
    """The feed a link names, resolved by its stable name when it has one."""
    return _feed_for(_FEEDLIST[_feed_index(idx, name)])


def _feed_for(spec):
    if spec.get("source") == "trending":
        return _trending_shows()
    if spec.get("browse"):
        return _tvmaze_shows(spec["browse"])
    return _latest()


def _latest_page(page):
    return get_json(_BASE_API, params={"limit": 100, "page": page})


def _latest():
    """
    The new releases, with each row carrying its show's poster.

    A page of releases names a few dozen shows, and several rows are the same
    show's episode in other qualities; the poster is looked up once per show
    and shared by its rows, which is what makes this page read like the
    trending and browse pages rather than a wall of identical icons.
    """
    torrents = _latest_page(1).get("torrents") or []
    shows = _shows_for(
        [t.get("imdb_id") for t in torrents], fresh_cap=_LATEST_FRESH
    )
    return _rows(torrents, shows=shows)


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _trending_shows():
    """
    The shows whose recent releases are being seeded most.

    EZTV has no top list to ask for, but every torrent carries its seeds and
    its show's IMDb id. The latest few pages are recent releases, so summing
    their seeds is "what people are watching now" as EZTV sees it -- and every
    show in the list is there because EZTV has it.
    """
    cached = _cached(("trending",), _TRENDING_TTL)
    if cached is not None:
        return cached

    shows = {}
    pages = _latest_pages(_TRENDING_PAGES)
    for data in pages:
        for torrent in data.get("torrents") or []:
            imdb = str(torrent.get("imdb_id") or "").strip()
            if not imdb or imdb == "0" or not torrent.get("magnet_url"):
                continue
            entry = shows.setdefault(
                imdb,
                {"names": {}, "seeds": 0, "releases": 0, "latest": (0, 0)},
            )
            title = torrent.get("title") or torrent.get("filename") or ""
            show_name = title_query(title).get("title") or ""
            if show_name:
                entry["names"][show_name] = entry["names"].get(show_name, 0) + 1
            entry["seeds"] += _int(torrent.get("seeds"))
            entry["releases"] += 1
            latest = (_int(torrent.get("season")), _int(torrent.get("episode")))
            if latest > entry["latest"]:
                entry["latest"] = latest

    ordered = sorted(
        shows.items(),
        key=lambda pair: (pair[1]["seeds"], pair[1]["releases"]),
        reverse=True,
    )[:_TRENDING_SHOWS]
    posters = _shows_for([imdb for imdb, _ in ordered])
    results = PlayItemList()
    for imdb, entry in ordered:
        show = posters.get(imdb)
        if entry["names"]:
            parsed_name = max(entry["names"].items(), key=lambda pair: pair[1])[0]
        else:
            parsed_name = "IMDb " + imdb
        season, episode = entry["latest"]
        bits = []
        if season and episode:
            bits.append("S%02dE%02d" % (season, episode))
        bits.append("%d releases" % entry["releases"])
        bits.append("%s seeds" % number_commas(entry["seeds"]))
        stats = " · ".join(bits)

        # The same look as the browse and search rows: the show's poster and
        # its own spelling of its name. A release name drops its punctuation
        # ("Georgie and Mandys First Marriage"), and EZTV has no artwork.
        if show:
            show_name = show.get("name") or parsed_name
            poster = (show.get("image") or {}).get("medium")
            subtitle = _show_subtitle(show)
            results.add(
                ShowMoreItem(
                    show_name,
                    poster or "/img/icons/tv.svg",
                    _show_link(imdb, show_name),
                    subtitle or stats,
                    stats if subtitle else None,
                )
            )
        else:
            results.add(
                ShowMoreItem(
                    parsed_name,
                    icon_for_title(parsed_name, "/img/icons/tv.svg"),
                    _show_link(imdb, parsed_name),
                    stats,
                )
            )
    _cache(("trending",), results)
    return results


def _latest_pages(count):
    """
    The latest pages together. They are one sample, not a queue: no page is
    newer than the time this feed takes to build, and reading them one after
    the other doubled the wait for nothing.

    The first page failing is the feed failing; a later one can be dropped,
    since the sample is only slightly smaller.
    """
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            page: pool.submit(_latest_page, page)
            for page in range(1, count + 1)
        }
    pages = []
    for page in range(1, count + 1):
        try:
            pages.append(futures[page].result())
        except Exception as exc:
            if page == 1:
                raise ApiError(
                    "EZTV did not answer. Try again. (%s)" % exc,
                    ApiError._SERVICE_UNAVAILABLE,
                )
    return pages


def _shows_for(imdbs, fresh_cap=None):
    """
    TVMaze entries for several shows, read together, as {imdb: show}.

    A show already cached costs nothing, so `fresh_cap` only bounds how many
    first-time lookups one build makes. A row whose show is not looked up yet
    keeps its icon, and has its poster from the cache on a later build.
    """
    imdbs = list(
        dict.fromkeys(str(i or "").strip() for i in imdbs if str(i or "").strip())
    )
    imdbs = [i for i in imdbs if i != "0"]
    if not imdbs:
        return {}
    if fresh_cap is not None:
        fresh = [i for i in imdbs if _cached(("show", i), _SHOW_TTL) is None]
        if len(fresh) > fresh_cap:
            keep = set(fresh[:fresh_cap])
            imdbs = [
                i for i in imdbs if i in keep or _cached(("show", i), _SHOW_TTL)
            ]
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        found = pool.map(_tvmaze_show, imdbs)
    return dict(zip(imdbs, found))


def _tvmaze_show(imdb):
    """
    A show's TVMaze entry, for its poster and its own name.

    Cached for a day, found or not: a poster does not change, and the trending
    feed asks about much the same shows each time it is built. A show EZTV has
    that TVMaze does not is fine -- the row falls back to the release name and
    an icon, which is how the feed looked before it had posters at all.
    """
    key = ("show", imdb)
    cached = _cached(key, _SHOW_TTL)
    if cached is not None:
        return cached or None
    try:
        show = get_json(_TVMAZE_LOOKUP, params={"imdb": "tt" + imdb})
    except Exception:
        show = None
    _cache(key, show or {})
    return show


def _card_rating(card):
    """The rating a TVMaze browse card prints, if it prints one."""
    text = get_text_content(card) or ""
    found = re.search(r"(\d(?:\.\d)?)\s*\(\s*([\d,]+)\s+votes?\)", text)
    if not found:
        return ""
    return "%s (%s votes)" % (found.group(1), found.group(2))


def _tvmaze_shows(params):
    """
    Shows from TVMaze's browse page, as rows that open their episodes.

    The cards give the id and the name; the poster and rating come with them.
    The TVMaze id is all the show page needs to resolve the IMDb id EZTV wants.
    """
    key = ("browse",) + tuple(sorted(params.items()))
    cached = _cached(key, _CACHE_TTL)
    if cached is not None:
        return cached

    results = PlayItemList()
    seen = set()
    for page in range(1, _SHOW_PAGES + 1):
        query = dict(params)
        if page > 1:
            query["page"] = str(page)
        try:
            doc = get_doc(_TVMAZE_BROWSE, params=query)
        except Exception as exc:
            if page == 1:
                raise ApiError(
                    "TVMaze did not answer. Try again. (%s)" % exc,
                    ApiError._SERVICE_UNAVAILABLE,
                )
            break
        for card in select_all(doc, "div[data-key]"):
            link = select_one(card, "span.title a")
            if link is None:
                continue
            match = re.search(r"/shows/(\d+)/", get_attr(link, "href") or "")
            if match is None:
                continue
            tvmaze_id = match.group(1)
            if tvmaze_id in seen:
                continue
            seen.add(tvmaze_id)
            show_name = (get_text(link) or "").strip()
            if not show_name:
                continue
            poster = select_one(card, "figure.image img")
            poster_url = get_attr(poster, "src") if poster is not None else None
            results.add(
                ShowMoreItem(
                    show_name,
                    poster_url or "/img/icons/tv.svg",
                    "tvmaze:" + tvmaze_id,
                    _card_rating(card),
                )
            )
        if not select_all(doc, "a[href*='page=%d']" % (page + 1)):
            break

    _cache(key, results)
    return results


def _show_link(imdb, show_name):
    """The show page link, with the name for the error if EZTV has nothing."""
    return "imdb:%s|%s" % (imdb, show_name)


def _item(t, imdb=None, show=None):
    """One torrent as a row, or None when it has no magnet to play."""
    magnet = t.get("magnet_url")
    if not magnet:
        return None
    title = (t.get("title") or t.get("filename") or "").strip()
    poster = (show.get("image") or {}).get("medium") if show else None
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
        released = datetime.fromtimestamp(int(released_unix), tz=timezone.utc).strftime(
            "%Y-%m-%d"
        )
    except (TypeError, ValueError, OSError, OverflowError):
        released = "NA"
    subtitle = "Size: " + size
    subtitle = subtitle + ", Seeds: " + seeds
    subtitle = subtitle + ", Released: " + released
    # A search or a show page knows which show this is, so Add Subtitles can
    # ask OpenSubtitles by id instead of by the release name.
    subs = {"imdb": "tt" + str(imdb)} if imdb else None
    return TorrentPlayItem(
        title,
        poster or icon_for_title(title, "/img/icons/tv.svg"),
        magnet,
        subtitle,
        subs=subs,
    )


def _rows(torrents, imdb=None, shows=None):
    """
    Torrents as rows, most seeded first.

    EZTV's API orders by release date. The number on every row is its seeds,
    and that is what the person is choosing between, so the list is ordered by
    it instead. Season and episode break ties, so equally seeded episodes
    still read newest first.
    """
    torrents = sorted(
        torrents,
        key=lambda t: (
            _int(t.get("seeds")),
            _int(t.get("season")),
            _int(t.get("episode")),
        ),
        reverse=True,
    )
    results = PlayItemList()
    for t in torrents:
        show = (shows or {}).get(str(t.get("imdb_id") or "").strip())
        item = _item(t, imdb=imdb, show=show)
        if item is not None:
            results.add(item)
    return results


def _episodes(imdb):
    """
    Every episode EZTV has for the show, up to a page cap.

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
        more = get_json(_BASE_API, params={"imdb_id": imdb, "limit": 100, "page": page})
        batch = more.get("torrents") or []
        if not batch:
            break
        torrents.extend(batch)
    return torrents


def _episode_rows(imdb, show_name, show=None):
    """
    One show's episodes, or a useful refusal.

    Every row carries the show's poster, so the page reads like the rest of
    the channel. The caller passes the show when it already has it; otherwise
    it is looked up, cached for a day like every other poster.
    """
    if show is None:
        show = _tvmaze_show(imdb)
    try:
        torrents = _episodes(imdb)
    except ApiError:
        raise
    except Exception as exc:
        raise ApiError(
            "EZTV did not answer for '%s'. Try again. (%s)" % (show_name, exc),
            ApiError._SERVICE_UNAVAILABLE,
        )
    torrents = [t for t in torrents if t.get("magnet_url")]
    if not torrents:
        raise ApiError(
            "EZTV has no episodes of '%s'. It may not carry the show, or the id "
            "may be wrong." % show_name,
            404,
        )
    return _rows(torrents, imdb=imdb, shows={imdb: show} if show else None)


def _search_matches(q):
    """TVMaze's matches that EZTV can be asked about, best first."""
    try:
        found = get_json(_TVMAZE_SEARCH, params={"q": q})
    except Exception as exc:
        raise ApiError(
            "Could not look up that show right now. Try again, or paste its "
            "IMDb id (tt...). (%s)" % exc,
            ApiError._SERVICE_UNAVAILABLE,
        )
    matches = []
    seen = set()
    for entry in found or []:
        show = entry.get("show") or {}
        imdb = (show.get("externals") or {}).get("imdb") or ""
        if imdb[:2] != "tt" or len(imdb) <= 2:
            continue
        digits = imdb[2:]
        if digits in seen:
            continue
        seen.add(digits)
        matches.append({"show": show, "imdb": digits})
    return matches


def _show_subtitle(show):
    """The line under a show in the picker: year, channel, genres, rating."""
    bits = []
    premiered = show.get("premiered") or ""
    if premiered[:4]:
        bits.append(premiered[:4])
    channel = show.get("network") or show.get("webChannel") or {}
    if channel.get("name"):
        bits.append(channel["name"])
    genres = show.get("genres") or []
    if genres:
        bits.append(", ".join(genres[:2]))
    rating = (show.get("rating") or {}).get("average")
    if rating:
        bits.append(str(rating))
    return " · ".join(bits)


def search(q):
    """
    A show's episodes, or the shows a title could mean.

    One match opens its episodes, as a title search used to. Several are a
    picker: "harry potter" is the upcoming series and a baking show, and the
    first of those has no episodes, which made the whole search look empty. A
    pasted IMDb id skips the lookup.
    """
    q = (q or "").strip()
    pasted = _IMDB_RE.match(q)
    if pasted:
        return _episode_rows(pasted.group(1), "IMDb " + pasted.group(1))

    matches = _search_matches(q)
    if not matches:
        raise ApiError(
            "No show found for '%s'. Try a different title, or paste its IMDb "
            "id (tt...)." % q,
            404,
        )
    if len(matches) == 1:
        match = matches[0]
        return _episode_rows(
            match["imdb"], match["show"].get("name") or q, show=match["show"]
        )

    results = PlayItemList()
    for match in matches[:20]:
        show = match["show"]
        poster = (show.get("image") or {}).get("medium")
        show_name = show.get("name") or q
        # Kept for the show page, which the row opens: the poster is then
        # already to hand and the page costs no lookup.
        _cache(("show", match["imdb"]), show)
        results.add(
            ShowMoreItem(
                show_name,
                poster or "/img/icons/tv.svg",
                _show_link(match["imdb"], show_name),
                _show_subtitle(show),
            )
        )
    return results


def showmore(link):
    """
    One show's episodes, from a browse row or a trending row.

    A browse row carries the TVMaze id, whose show entry names the IMDb id;
    a trending row already has the IMDb id. Both end at the same episode list.
    """
    link = (link or "").strip()
    show_name = "this show"
    show = None
    if link.startswith("tvmaze:"):
        tvmaze_id = link.split(":", 1)[1].strip()
        try:
            show = get_json(_TVMAZE_SHOW % tvmaze_id)
        except Exception as exc:
            raise ApiError(
                "TVMaze did not answer. Try again. (%s)" % exc,
                ApiError._SERVICE_UNAVAILABLE,
            )
        show_name = show.get("name") or show_name
        imdb = (show.get("externals") or {}).get("imdb") or ""
        if imdb[:2] != "tt" or len(imdb) <= 2:
            raise ApiError(
                "TVMaze has no IMDb id for '%s', and EZTV needs one." % show_name,
                404,
            )
        digits = imdb[2:]
    elif link.startswith("imdb:"):
        value = link.split(":", 1)[1].strip()
        if "|" in value:
            value, show_name = value.split("|", 1)
        match = _IMDB_RE.match(value.strip())
        if match is None:
            raise ApiError("That show link is not understood.", 400)
        digits = match.group(1)
    else:
        raise ApiError("That show link is not understood.", 400)
    # A browse row's TVMaze entry is already in hand; a trending or picker row
    # leaves the poster to the day cache, which is where it got its own.
    return _episode_rows(digits, show_name, show=show)

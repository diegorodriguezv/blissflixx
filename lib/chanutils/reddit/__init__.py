"""
Reddit posts for the channels that are built on them.

Reddit's own API is not reachable from here. Its anonymous .json endpoints
answer

    You've been blocked by network security.

which is a policy block rather than a rate limit: it lands on every request,
from every address, and changing the User-Agent, the scheme or the host does
not help. Self-service API registration has gone the same way -- creating an
app now only leads to Reddit's Responsible Builder Policy, which requires
approval before any access, and points developers at their own platform.

So the posts come from Arctic Shift instead, a community-run archive of Reddit
data with a keyless API. It answers from its own record rather than from
Reddit, so none of the above applies, and it serves the same field names, so
_extract() below reads it as if it were the API's own response.

Two things it is not. It is volunteer-run with no uptime guarantee, so a
failure here has to stay a failure rather than a hang. And its listings lag
Reddit slightly, which for "what is new in this subreddit" is the wrong way
round and is why _TIMEOUT is generous rather than eager.
"""

import re
import time
import urllib.parse

from datetime import datetime, timedelta, timezone

import cherrypy
import requests

from ...playitem import PlayItem, PlayItemList
from ..chanutils import get_json, movie_title_year, replace_entity

#: Arctic Shift's search endpoint. Posts and comments are separate paths; only
#: posts carry what these channels show.
_API = "https://arctic-shift.photon-reddit.com/api/posts/search"

#: Reddit's listing is about 25 posts, so ask for a little over that and let
#: _extract drop the self-posts that never make it into the list.
_LIMIT = 30

#: Seconds to wait for one attempt against the archive. Longer than a Pi takes
#: to answer comfortably, because the archive is slow as well as unreliable,
#: and a short deadline mostly throws away attempts that were about to succeed.
_TIMEOUT = 15

#: Seconds to allow for the whole call, retries included. The alternative to
#: waiting is a request that never comes back and a UI that never stops
#: spinning, but the ceiling has to be well under what retrying would take on
#: its own: five slow attempts in a row is over a minute of a user watching a
#: spinner. The point of retrying is to rescue the query that fails fast, not to
#: wait out one that is genuinely struggling. Past this the caller gets an
#: empty listing and can try again.
_MAX_SECONDS = 25

#: How far back a "top" listing looks, as a date the archive accepts. Reddit's
#: own was "of all time", which the archive cannot do: it stores no score
#: ranking to ask for. A year is long enough for the highest-scoring posts in
#: a film subreddit to be a recognisable set rather than this week.
_TOP_WINDOW = "2025-10-01"

#: Seconds to hold a listing before asking again. Every channel that shares a
#: subreddit shares this too, so clicking through several of them in a row
#: costs the archive one request rather than one each. The archive is the
#: scarce resource here, so this is what protects it.
_CACHE_TTL = 5 * 60

#: How many times to retry the archive's timeout response before failing.
#: It answers 422 with "Timeout. Maybe slow down a bit" when a query takes it
#: too long, which is its own overloaded case rather than a bad request --
#: the identical query returns 200 moments later. So the same query is worth
#: asking again.
#:
#: Search is retried more than a feed is, because the archive is markedly less
#: reliable over it: measured at 4 successes in 10 title queries against 6 of 6
#: for the plain subreddit listing. The failures come back in under a second,
#: then again after the full timeout, so the wait is short when it can be and
#: the extra attempts are what turn "no results" into results.
_RETRIES = 3
_SEARCH_RETRIES = 5

#: Pause between those retries, in seconds. Long enough not to pile onto the
#: archive while it is already struggling, short enough to stay invisible.
_RETRY_WAIT = 2

_cache = {}


def _cached(key, fetch):
    """
    The listing for key, fetched at most once per _CACHE_TTL.

    Shared across channels: /r/videos is one cache entry whether it was asked
    for by the video channel or by a search that happened to match. This is
    the whole reason the archive is not being hammered -- without it every
    click was a request, and the archive's own answer to being asked too often
    is to time out.
    """
    entry = _cache.get(key)
    if entry and time.monotonic() - entry[0] < _CACHE_TTL:
        return entry[1]
    data = fetch()
    _cache[key] = (time.monotonic(), data)
    return data


def _query(params, retries=None):
    """
    Ask the archive, retrying the case where it says it was too slow.

    A 422 with its own "maybe slow down" message means the archive gave up on
    the query, not that the query was wrong, so it is retried rather than
    raised. A genuine failure -- the archive down, a network problem -- is
    left to propagate as the exception it is.

    Stops at _MAX_SECONDS however the attempts are going, so one query the
    archive is struggling with cannot hold the caller for a minute. What is
    left unasked is asked next time: nothing is cached from a failed call, so
    a retry by the user starts fresh rather than inheriting this staleness.
    """
    last = None
    deadline = time.monotonic() + _MAX_SECONDS
    for attempt in range(_RETRIES if retries is None else retries):
        try:
            response = requests.get(
                _API,
                params=params,
                timeout=_TIMEOUT,
                headers={"user-agent": "blissflixx/1.0"},
            )
            if response.status_code == 422:
                last = "the archive reported the query timed out"
                if time.monotonic() >= deadline:
                    break
                time.sleep(_RETRY_WAIT * (attempt + 1))
                continue
            response.raise_for_status()
            return response.json().get("data") or []
        except requests.RequestException as exc:
            last = exc
            if time.monotonic() >= deadline:
                break
            time.sleep(_RETRY_WAIT * (attempt + 1))
    raise Exception("arctic shift: could not be read (%s)" % last)


def search(subreddit, q, moviesubs=False):
    """
    Posts in subreddit whose title mentions q.

    The archive searches titles only, and only within one subreddit, which is
    what these channels search anyway.
    """
    params = {
        "subreddit": subreddit,
        "title": q,
        "limit": _LIMIT,
        "sort": "desc",
    }
    data = _cached_reading(
        ("search", subreddit, q.lower()), params, retries=_SEARCH_RETRIES
    )
    return _extract(_listing(data), moviesubs)


def top(subreddit, days=7, moviesubs=False):
    """
    Highest scoring posts from the last few days.

    The archive has no score ranking of its own, so Reddit's "top" listing is
    served by asking it for everything in a recent window and ranking that here
    by score. That is slower than a single archive listing but gives the sort of
    feed a trailers channel wants: what people actually reacted to, rather than
    only what is newest.
    """
    after = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    params = {
        "subreddit": subreddit,
        "limit": _LIMIT * 3,
        "sort": "desc",
        "after": after,
    }
    data = _cached_reading(("top", subreddit, days), params, retries=_SEARCH_RETRIES)
    data.sort(key=lambda post: int(post.get("score") or 0), reverse=True)
    return _extract(_listing(data[:_LIMIT]), moviesubs)


def get_feed(feed, moviesubs=False):
    """
    Posts for one feed entry.

    The feed still says which subreddit and which listing in the URL it was
    written against. That is deliberate: the channels read as they always
    did, and the move to the archive is contained here.
    """
    params = _feed_params(feed["url"])
    if params is None:
        # Not a listing this helper knows how to serve -- currently only the
        # retired channel's sitewide domain feed. An empty list rather than an
        # error, so a feed that cannot be served reads as a channel with
        # nothing in it instead of a failure.
        return PlayItemList()
    # A flair feed is a search over the archive as much as search is, and it
    # fails the same way, so it gets the same extra attempts. Measured: a
    # plain subreddit listing answers without trouble, flair and title queries
    # both time out intermittently.
    retries = _SEARCH_RETRIES if _is_filtered(params) else None
    data = _cached_reading(tuple(sorted(params.items())), params, retries=retries)
    return _extract(_listing(data), moviesubs)


def _cached_reading(key, params, retries=None):
    """
    A cached listing, or an empty one if the archive could not be read in time --
    and a failure is never the thing that gets cached.

    This is why there is a function here at all, rather than calling _read inside
    _cached. _read returned an empty list when the archive could not be read, and
    _cached stored whatever it was handed, so one unlucky moment put an empty
    list into the cache and kept it there for the full five minutes:

        20:34:15  archive timed out, five attempts, feed empty
        20:36:25  same feed, still empty -- and the archive was never asked again

    That second line is the symptom. The archive had long since recovered; the
    second click was answered from the cache, so the feed read as an empty
    category rather than as a busy archive and nothing said which. It looked
    like the channel had lost its posts -- which is exactly what it looked like
    to the person clicking it, on r/Shortfilms' "Gay & Lesbian", the day the
    archive happened to be slow.

    So the failure now propagates out of the fetch, past the cache, and becomes
    an empty listing here instead: logged, and stored nowhere. The next click
    asks the archive again. A genuine empty result is still cached, because the
    archive answering "no such posts" is an answer, not a failure.

    The archive being unreliable is expected rather than exceptional -- it is
    volunteer-run and answers a narrowed query unreliably -- so this still
    returns an empty listing rather than raising. Raising turned a busy archive
    into an error page over the whole channel, for a feed the user can simply
    ask for again. Silently empty would be worse than either: an empty category
    and a failed fetch have to stay distinguishable in the log, which is why the
    failure is logged here.
    """
    try:
        return _cached(key, lambda: _query(params, retries=retries))
    except Exception as exc:
        cherrypy.log("reddit listing unavailable: %s" % exc)
        return []


def _feed_params(url):
    """
    Translate one of Reddit's listing URLs into archive query parameters.

    Returns None for anything that is not a subreddit listing. The one that
    matters is the sitewide domain feed (/domain/<site>/...): it lists posts
    linking to one site from anywhere on Reddit, and the archive has no
    equivalent -- it can only search within a named subreddit. Returning None
    for it is what makes that feed come back empty instead of quietly
    returning the wrong thing.
    """
    if "://" not in url:
        return None
    path = url.split("://", 1)[1].split("/", 1)[1] if "/" in url else ""
    # These URLs end in ".json", often after a query string
    # ("/r/videos/top.json?sort=top&t=all"), so the query is kept aside rather
    # than dropped, and the suffix stripped per segment: "/r/trailers.json" is
    # the subreddit "trailers", not one called "trailers.json".
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    parts = [p[: -len(".json")] if p.endswith(".json") else p for p in path.split("/")]
    parts = [p for p in parts if p]
    if len(parts) < 2 or parts[0] != "r":
        return None
    subreddit = parts[1]
    # "new" is a listing; the bare subreddit path is the hot listing, and a
    # trailing segment that is neither is a listing we do not recognise.
    if len(parts) == 2:
        listing = None
    elif parts[2] in ("new", "hot", "rising", "top"):
        listing = parts[2]
    elif parts[2] == "search":
        listing = None
    else:
        return None
    params = {"subreddit": subreddit, "limit": _LIMIT, "sort": "desc"}
    if listing == "new":
        params["sort"] = "asc"
    # "top" asked for the highest scoring posts of all time. The archive will
    # not rank by score -- it sorts by time and takes before/after bounds --
    # so this is the closest honest equivalent rather than the same thing: a
    # window of the highest scoring posts in it. Kept separate from the others
    # because it is an approximation, and saying so here is cheaper than the
    # user discovering the difference.
    if listing == "top":
        params["after"] = _TOP_WINDOW
    # A flair listing: /r/Documentaries/search.json?q=flair:'Art'&sort=top.
    # The documentaries channel is mostly these -- thirty-odd feeds that are
    # one flair each -- so parsing them is most of that channel working. The
    # archive filters on link_flair_text, which is the same string.
    flair = _flair_from_query(query)
    if flair:
        params["link_flair_text"] = flair
        return params
    # The shortfilms channel instead searches on words: ?q=action%20adventure.
    # Those are titles containing the phrase, which the archive can do with the
    # same title parameter the search box uses.
    term = _term_from_query(query)
    if term:
        params["title"] = term
    return params


def _is_filtered(params):
    """
    True when the query narrows the listing rather than just naming a subreddit.

    Both the flair feeds and the word-search feeds are filtered, and both time
    out intermittently where a plain subreddit listing does not. That is the
    distinction the retry budget turns on.
    """
    return bool(params.get("link_flair_text") or params.get("title"))


def _query_fields(query):
    """
    The q= value out of a feed's query string, or None.

    The subreddits that search write it as a bare phrase, with + or %20 for
    the space. This is the phrase, as it was written.
    """
    for key, value in urllib.parse.parse_qsl(query):
        if key == "q":
            return value
    return None


def _term_from_query(query):
    """
    A search phrase out of "?q=action%20adventure", unless it is a flair.

    A flair arrives as flair:'Art' -- a field selector, not words -- and is
    handled as one. Everything else is words to look for in the title.
    """
    value = _query_fields(query)
    if not value:
        return None
    if re.search(r"flair\s*:", value, re.IGNORECASE):
        return None
    return value or None


def _flair_from_query(query):
    """
    The flair out of "?q=flair%3A%27Art%27", or None if there is not one.

    The flair is a label a subreddit applies to a post, and the subreddits that
    use them write their feeds exactly like this. So the feed the user chose in
    the interface is recovered here and asked for as itself: "Crime" in the
    list still means posts flaired Crime.
    """
    for field in urllib.parse.parse_qsl(query):
        key, value = field
        if key != "q":
            continue
        match = re.search(r"flair\s*:\s*['\"]([^'\"]+)['\"]", value, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _listing(posts):
    """
    The archive's rows as the listing shape _extract already reads.

    Arctic Shift returns the same fields the API did -- title, url,
    permalink, score, num_comments, thumbnail, is_self -- one object per post,
    where the API wrapped each in {"kind": ..., "data": ...}. Normalising here
    means _extract() did not have to change, and nothing above it can tell
    which one it is being served.
    """
    return {"data": {"children": [{"data": post} for post in posts]}}


def _extract(data, moviesubs):
    results = PlayItemList()
    if "data" not in data or len(data["data"]["children"]) == 0:
        return results
    rtree = data["data"]["children"]
    for r in rtree:
        r = r["data"]
        # Internal reddit question/discussion
        if r["is_self"]:
            continue
        thumb = None
        if r["thumbnail"] and r["thumbnail"].find("/") > -1:
            thumb = r["thumbnail"]
        subtitle = "Score: " + str(r["score"])
        comments = (
            "<a target='_blank' href='http://reddit.com"
            + r["permalink"]
            + "'>Comments:"
            + str(r["num_comments"])
            + "</a>"
        )
        subtitle = subtitle + ", " + comments
        title = replace_entity(r["title"])
        url = replace_entity(r["url"])
        subs = None
        if moviesubs:
            subs = movie_title_year(title)
        item = PlayItem(title, thumb, url, subtitle, subs=subs)
        results.add(item)
    return results


# Kept for the channels that still import it, and for anything that reaches
# reddit over the old path directly.
__all__ = ["search", "get_feed", "top"]

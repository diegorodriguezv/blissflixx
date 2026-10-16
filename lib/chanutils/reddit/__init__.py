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
#: spinning. Every attempt is bounded by what is left of this, so it is a real
#: ceiling rather than the gaps between attempts.
#:
#: It used to be 25, and it was not the ceiling it claimed to be: the deadline
#: was only checked between attempts and each attempt was allowed the full
#: _TIMEOUT, so two slow attempts came to about 32 seconds. Fifteen is still
#: longer than any single successful attempt needs -- a plain listing answers in
#: under two seconds and even a successful narrowed query took about seven when
#: measured -- and it leaves room for the full retry budget on the queries that
#: fail fast, which is the case worth retrying.
_MAX_SECONDS = 15

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

#: Pause between retries, in seconds. Flat, and short.
#:
#: This used to escalate -- 2, then 4, then 6, then 8 -- which spent twenty of
#: the twenty-five second budget asleep and turned a recoverable blip into half
#: a minute of nothing happening. It bought nothing: the archive answers its own
#: overload in about a second and a half, and the identical query works moments
#: later, so what a retry needs is a small gap, not a growing one.
#:
#: A second is also the right size for the person waiting. It keeps the gap
#: invisible next to the request itself, and it stops a burst of feed-clicks
#: from queueing up behind each other's sleeps -- each feed is a distinct query,
#: so nothing is served from cache to save the wait.
_RETRY_WAIT = 1


class ArchiveBusy(Exception):
    """
    The archive could not be read, and the caller is told so.

    This exists so that "the archive is busy" and "this category has no posts"
    stop being the same thing on screen. They used to be: a failed read became
    an empty listing, the interface showed "There are no items for this feed.",
    and a category that has always had films in it looked like it had lost them.
    Nothing on screen distinguished the two, and the only evidence was a line in
    a log file nobody opens.

    It is raised rather than returned so it can travel: lib.api.channels turns it
    into an ApiError, and the interface has an error page for exactly this. The
    message is written for the person reading it rather than for the log, which
    is why it is built here instead of by the layer that catches it.

    Search across every channel catches this and skips the channel that failed,
    so one busy archive cannot empty a search of forty others.
    """


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


def _split_queries(params):
    """
    The requests one feed needs, as a list of parameter dicts.

    A feed normally needs exactly one. The exception is a feed naming more than
    one flair, as "Misc/Adult" does with

        flair:Misc OR flair:Adult

    The archive cannot be asked for either of those: link_flair_text=Misc OR
    Adult and link_flair_text=Misc,Adult both come back 422, and sending the key
    twice is a 400. It is a single-value filter and that is the whole of it, so
    the OR is done here instead -- one request per flair, rows merged below.

    The flairs are carried in params as a tuple, which _feed_params writes and
    this reads, so that the cache key covers the whole set: a two-flair feed must
    not share a cache entry with either of its halves.
    """
    flairs = params.get("link_flair_text")
    if not isinstance(flairs, tuple):
        return [params]
    queries = []
    for flair in flairs:
        one = dict(params)
        one["link_flair_text"] = flair
        queries.append(one)
    return queries


def _query(params, retries=None):
    """
    Ask the archive for one feed, which may be more than one request.

    Each query is asked on its own, retrying the case where the archive says it
    was too slow. A 422 with its own "maybe slow down" message means the archive
    gave up on the query, not that the query was wrong, so it is retried rather
    than raised. A genuine failure -- the archive down, a network problem -- is
    left to propagate as the exception it is.

    Stops at _MAX_SECONDS however the attempts are going, so one query the
    archive is struggling with cannot hold the caller for a minute. What is
    left unasked is asked next time: nothing is cached from a failed call, so
    a retry by the user starts fresh rather than inheriting this staleness.

    The rows from a multi-flair feed are merged here rather than left to the
    channel, because a post can carry both flairs ("Action | Drama") and would
    otherwise appear twice in the same feed.
    """
    queries = _split_queries(params)
    if len(queries) == 1:
        return _query_one(queries[0], retries=retries)

    rows = []
    seen = set()
    for query in queries:
        for post in _query_one(query, retries=retries):
            # The id is what identifies a post; the permalink is the fallback
            # for a row that somehow arrives without one.
            key = post.get("id") or post.get("permalink")
            if key in seen:
                continue
            seen.add(key)
            rows.append(post)
    # Each query comes back newest-first on its own, so merging two of them
    # interleaves them unless they are put back in one order.
    rows.sort(key=lambda post: post.get("created_utc") or 0, reverse=True)
    # Two queries means up to twice _LIMIT rows, and a feed is a screenful: left
    # alone this one feed would show 60 films where every other shows 30. Capped
    # here rather than in _query_one, which does not cap -- a single query is
    # already bounded by the limit asked for, and top() deliberately asks for
    # three times that and needs every row it gets.
    return rows[:_LIMIT]


def _query_one(params, retries=None):
    """
    One request's worth: ask the archive, retrying the case where it says it was
    too slow, and return its rows.

    Split out of _query() so that a feed made of more than one request can merge
    them; the retry logic itself belongs to the single request.

    Every attempt is bounded by what is left of _MAX_SECONDS rather than by
    _TIMEOUT outright. That is the difference between the ceiling being real and
    being only the gaps between attempts: with the full timeout allowed each
    time, a slow archive could take two attempts and thirty-two seconds through a
    budget written as twenty-five. An attempt that would run past the deadline is
    not started at all.
    """
    last = None
    deadline = time.monotonic() + _MAX_SECONDS
    attempts = _RETRIES if retries is None else retries
    for attempt in range(attempts):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Pause only when there is going to be another attempt. Sleeping after
        # the last one is a second added to every total failure -- the caller is
        # already giving up and is simply made to wait before it hears so.
        another_attempt = attempt < attempts - 1
        try:
            response = requests.get(
                _API,
                params=params,
                timeout=min(_TIMEOUT, remaining),
                headers={"user-agent": "blissflixx/1.0"},
            )
            if response.status_code == 422:
                last = "the archive reported the query timed out"
                if another_attempt:
                    _pause_before_retry(deadline)
                continue
            response.raise_for_status()
            return response.json().get("data") or []
        except requests.RequestException as exc:
            last = exc
            if another_attempt:
                _pause_before_retry(deadline)
    raise ArchiveBusy(
        "The Reddit archive is busy and did not answer. "
        "This is not a fault of this server -- try again in a moment. (%s)" % last
    )


def _pause_before_retry(deadline):
    """
    Wait before the next attempt, but never past the deadline.

    The sleep used to run to completion even when it began near the end of the
    budget, so a 422 arriving in the last moment still cost its full wait and
    pushed the caller past _MAX_SECONDS. Truncated to what is left, it is a
    pause rather than an overrun.
    """
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(_RETRY_WAIT, remaining))


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
    A cached listing, or ArchiveBusy if the archive could not be read.

    Two things are arranged here, and both were wrong before.

    The first is that a failure must never be cached. An earlier version caught
    the failure and returned an empty list, and _cached stored whatever it was
    handed, so one unlucky moment put an empty list into the cache and kept it
    there for the full five minutes:

        20:34:15  archive timed out, five attempts, feed empty
        20:36:25  same feed, still empty -- and the archive was never asked again

    That second line is the symptom. The archive had long since recovered; the
    second click was answered from the cache, so the feed read as an empty
    category rather than as a busy archive and nothing said which. It looked
    like the channel had lost its posts -- which is exactly what it looked like
    to the person clicking it, on r/Shortfilms' "Gay & Lesbian", the day the
    archive happened to be slow.

    The second is that a failure has to be visible. Turning one into an empty
    listing is what made "the archive is busy" and "this category has no posts"
    the same thing on screen. It is raised instead, and raised rather than
    returned so that it stays distinguishable from an empty answer all the way
    to the interface, which has an error page for exactly this.

    A genuine empty result is still cached: the archive answering "no such posts"
    is an answer, and caching it is what stops every click hammering a
    volunteer-run archive.
    """
    try:
        return _cached(key, lambda: _query(params, retries=retries))
    except ArchiveBusy as exc:
        cherrypy.log("reddit listing unavailable: %s" % exc)
        raise


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
    flairs = _flairs_from_query(query)
    if flairs:
        # A tuple when the feed names more than one, because the archive has no
        # OR; _split_queries() turns that into one request each. See it.
        params["link_flair_text"] = flairs[0] if len(flairs) == 1 else tuple(flairs)
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


def _flairs_from_query(query):
    """
    Every flair named in a "?q=..." filter, in the order written.

    The flair is a label a subreddit applies to a post, and the subreddits that
    use them write their feeds exactly like this. So the feed the user chose in
    the interface is recovered here and asked for as itself: "Crime" in the list
    still means posts flaired Crime.

    Both spellings Reddit accepts are read, and the difference matters:

        flair:'Art'      what bfch_r_documentaries writes, quoted
        flair:Action     what bfch_r_fullmoviesonyoutube writes, unquoted

    Only the quoted one used to be recognised, and the effect of missing the
    unquoted one was not an error -- it was a feed with no filter on it at all.
    All 22 of the fullmoviesonyoutube flair feeds were the same unfiltered
    /r/fullmoviesonyoutube listing: picking "Crime" showed the same thirty
    newest posts as "Western", and there was nothing on screen to say so.
    _term_from_query() does not rescue it either; it deliberately refuses to read
    "flair:Action" as title words, so the filter simply vanished.

    More than one flair is returned for a feed that ORs them:

        flair:Misc OR flair:Adult

    The archive cannot be asked for that in one request; see _split_queries().
    """
    for field in urllib.parse.parse_qsl(query):
        key, value = field
        if key != "q":
            continue
        # Either a quoted value or a bare one running to whitespace, whichever
        # comes first, so both spellings are taken from the same expression.
        found = re.findall(
            r"flair\s*:\s*(?:['\"]([^'\"]+)['\"]|([^\s'\"]+))", value, re.IGNORECASE
        )
        names = [quoted or bare for quoted, bare in found]
        if names:
            return names
    return []


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


#: A link to a YouTube channel rather than to a video: youtube.com/@handle, and
#: the older /channel/, /c/ and /user/ forms.
#:
#: Measured across seven subreddits: 1 post in 100 in /r/youtubevideos, where
#: they are posted for a reason ("we are live", "new video out"), and 1 in 100
#: in /r/Shortfilms. Rare enough to look like nothing, and not: yt-dlp answers a
#: channel URL by walking that channel's entire video list, which is minutes of
#: work it does not finish. Nothing here is waiting for it either -- ytdlproc's
#: _ready() reads yt-dlp's output in a loop with no timeout -- so such an item
#: does not fail and does not start. It sits there, and the only way out is
#: pressing stop.
#:
#: Dropped here rather than in the channels, because every Reddit channel goes
#: through _extract(), and a filter that lives in one channel is a filter the
#: next channel forgets. A YouTube channel link is never a playable video, so
#: there is nothing to lose by dropping it everywhere.
#:
#: A video link is fine in every form that resolves -- youtu.be, /watch?v=,
#: /shorts/, /embed/, and a playlist, which resolves to its first entry because
#: the player passes --no-playlist.
_CHANNEL_URL = re.compile(
    r"^https?://(?:[\w-]+\.)*youtube\.com/(?:@[\w.-]+|channel/|c/|user/)"
)


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
        # Dropped here, before the item is built, rather than by each channel
        # that asks for a listing. An item that cannot play cannot be explained
        # once it is on screen looking as playable as the rest, so it is never
        # built. See _CHANNEL_URL for what happens to one that is left in.
        if not url or _CHANNEL_URL.match(url):
            continue
        subs = None
        if moviesubs:
            subs = movie_title_year(title)
        item = PlayItem(title, thumb, url, subtitle, subs=subs)
        results.add(item)
    return results


# Kept for the channels that still import it, and for anything that reaches
# reddit over the old path directly.
__all__ = ["search", "get_feed", "top"]

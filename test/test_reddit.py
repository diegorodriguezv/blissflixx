"""
The Reddit-backed channels.

lib.chanutils.reddit talks to Arctic Shift rather than Reddit's own API, so a
lot of what these channels do is translating a Reddit-style listing into
archive query parameters and then turning the archive's rows back into play
items. That translation is pure and worth pinning down; a change to it silently
empties a channel rather than raising.
"""

import time
import unittest.mock as m

import pytest

import lib.chanutils.reddit as reddit


def _post(title, score, url="http://example/x"):
    return {
        "title": title,
        "url": url,
        "permalink": "/r/trailers/comments/x/" + title.lower().replace(" ", "_"),
        "score": score,
        "num_comments": 1,
        "thumbnail": "http://example/thumb.jpg",
        "is_self": False,
    }


class TestRedditFeedParams:
    def test_hot_subreddit_listing_is_newest_first(self):
        params = reddit._feed_params("http://www.reddit.com/r/trailers.json")
        assert params["subreddit"] == "trailers"
        assert params["sort"] == "desc"

    def test_new_listing_is_newest_first(self):
        """
        It used to be "asc", which is the oldest posts in the whole archive and
        not the newest. Every Reddit channel was serving 2012 under the name
        New: /r/videos offered "Short film about love and robots." as new.

        This test used to assert asc, with a docstring explaining that asc
        gives the oldest posts. It described the bug instead of catching it,
        which is the one thing a test written that way cannot do.
        """
        params = reddit._feed_params("http://www.reddit.com/r/trailers/new.json")
        assert params["sort"] == "desc"

    def test_top_listing_uses_a_recent_window(self):
        params = reddit._feed_params("http://www.reddit.com/r/trailers/top.json")
        assert params["after"] == reddit._top_window_date()

    def test_the_top_window_rolls_rather_than_drifting(self):
        """
        It was the literal "2025-10-01", so the window grew by a day every day
        and the feed got slower and less relevant the longer a server was left
        running. A fixed date in a window described as "a year" stops being a
        year on the day it is written.
        """
        from datetime import datetime, timedelta, timezone

        # Compared in UTC because that is what _top_window_date() uses.
        # date.today() is local, and this was written with date.today(), so it
        # passed until the clock crossed UTC midnight and local and UTC dates
        # disagreed -- a test that fails once a day for a reason that has
        # nothing to do with what it is testing.
        assert reddit._top_window_date() == (
            datetime.now(timezone.utc) - timedelta(days=365)
        ).date().isoformat()

    def test_sitewide_domain_feeds_are_not_served(self):
        assert (
            reddit._feed_params("http://www.reddit.com/domain/vimeo.com/.json") is None
        )


class TestFlairFeedsAreFiltered:
    """
    A feed that names a flair has to ask the archive for that flair.

    Reddit accepts the value quoted or bare, and the channels use both:

        bfch_r_documentaries       flair:'Art'      quoted
        bfch_r_fullmoviesonyoutube  flair:Action     bare

    Only the quoted form used to be recognised, and a missed filter is silent:
    the feed falls back to the whole subreddit, so every flair feed in a channel
    returns the identical listing. Picking "Crime" showed the same thirty newest
    posts as "Western", with nothing on screen to say the choice was ignored.
    """

    def _feeds(self, module):
        return {f["title"]: f for f in module.feedlist()}

    def test_a_quoted_flair_is_the_filter(self):
        params = reddit._feed_params(
            "http://www.reddit.com/r/Documentaries/search.json"
            "?q=flair%3A%27Art%27&sort=top"
        )
        assert params["link_flair_text"] == "Art"

    def test_a_bare_flair_is_the_filter_too(self):
        """
        The regression. Twenty-two of the twenty-three feeds on
        bfch_r_fullmoviesonyoutube are written this way and every one of them
        was reaching the archive with no filter on it.
        """
        params = reddit._feed_params(
            "http://www.reddit.com/r/fullmoviesonyoutube/search.json"
            "?q=flair%3AAction&sort=top&restrict_sr=on"
        )
        assert params["link_flair_text"] == "Action"
        assert "title" not in params

    def test_every_flair_feed_of_a_channel_reaches_a_different_query(self):
        from chls import bfch_r_fullmoviesonyoutube as movies

        queries = set()
        for feed in movies.feedlist():
            params = reddit._feed_params(feed["url"])
            assert params is not None, feed["title"]
            queries.add(tuple(sorted(params.items(), key=lambda kv: kv[0])))

        # 23 feeds: Latest, 21 single flairs, and Misc/Adult which names two.
        assert len(queries) == len(
            movies.feedlist()
        ), "these feeds are not distinct queries; they will show the same posts"

    def test_the_documentaries_channel_was_already_distinct(self):
        """
        The other side of it: the channel that quotes its flairs was never
        affected, which is why this went unnoticed for so long.
        """
        from chls import bfch_r_documentaries as docs

        queries = set()
        for feed in docs.feedlist():
            params = reddit._feed_params(feed["url"])
            if params is None:
                continue
            queries.add(tuple(sorted(params.items(), key=lambda kv: kv[0])))
        assert len(queries) == len(docs.feedlist())


class TestAFeedNamingTwoFlairs:
    """
    "Misc/Adult" is flair:Misc OR flair:Adult, and the archive has no OR for it.

    link_flair_text is a single-value filter: "Misc OR Adult" and "Misc,Adult"
    both come back 422, and sending the key twice is a 400. So the feed is asked
    as two requests and merged here.
    """

    def _or_feed(self):
        return {
            "title": "Misc/Adult",
            "url": "http://www.reddit.com/r/fullmoviesonyoutube/search.json"
            "?q=flair%3AMisc+OR+flair%3AAdult&sort=top&restrict_sr=on",
        }

    def test_both_flairs_are_found_and_kept_in_order(self):
        params = reddit._feed_params(self._or_feed()["url"])
        assert params["link_flair_text"] == ("Misc", "Adult")

    def test_a_single_flair_is_not_wrapped_in_a_tuple(self):
        """
        The tuple is what marks a feed as needing more than one request, so one
        flair must stay a plain string or every flair feed would be doubled.
        """
        params = reddit._feed_params(
            "http://www.reddit.com/r/fullmoviesonyoutube/search.json?q=flair%3AAction"
        )
        assert params["link_flair_text"] == "Action"

    def test_it_becomes_one_request_each(self):
        queries = reddit._split_queries(reddit._feed_params(self._or_feed()["url"]))
        assert [q["link_flair_text"] for q in queries] == ["Misc", "Adult"]

    def test_a_single_flair_stays_one_request(self):
        params = reddit._feed_params(
            "http://www.reddit.com/r/fullmoviesonyoutube/search.json?q=flair%3AAction"
        )
        assert len(reddit._split_queries(params)) == 1

    def test_the_two_listings_are_merged_without_duplicates(self, monkeypatch):
        """
        A post can carry both flairs -- "Action | Drama" is a real flair value
        here -- so the same film comes back from both requests and must appear
        once, not twice in one feed.
        """
        monkeypatch.setattr(reddit, "_cache", {})
        shared = _post("Action | Drama", 10)
        shared["id"] = "same"
        shared["created_utc"] = 100
        only_misc = _post("just misc", 5)
        only_misc["id"] = "misc"
        only_misc["created_utc"] = 50
        only_adult = _post("just adult", 5)
        only_adult["id"] = "adult"
        only_adult["created_utc"] = 75

        asked = []

        def fake_query_one(params, retries=None):
            asked.append(params["link_flair_text"])
            return {"Misc": [shared, only_misc], "Adult": [shared, only_adult]}[
                params["link_flair_text"]
            ]

        monkeypatch.setattr(reddit, "_query_one", fake_query_one)

        items = reddit.get_feed(self._or_feed()).to_list()

        assert asked == ["Misc", "Adult"]
        assert [i.title for i in items] == [
            "Action | Drama",
            "just adult",
            "just misc",
        ]

    def test_the_merged_feed_is_still_one_screenful(self, monkeypatch):
        """
        Two requests is up to sixty rows, and every other feed shows thirty.
        A feed that showed sixty would look like a different kind of feed
        rather than the same one, twice the length.
        """
        monkeypatch.setattr(reddit, "_cache", {})

        def fake_query_one(params, retries=None):
            flair = params["link_flair_text"]
            posts = []
            for n in range(reddit._LIMIT):
                post = _post("%s %d" % (flair, n), 1)
                post["id"] = "%s-%d" % (flair, n)
                post["created_utc"] = n
                posts.append(post)
            return posts

        monkeypatch.setattr(reddit, "_query_one", fake_query_one)

        assert len(reddit.get_feed(self._or_feed()).to_list()) == reddit._LIMIT

    def test_a_two_flair_feed_does_not_share_a_cache_entry(self, monkeypatch):
        """
        The cache key is built from the params, and it has to cover both flairs:
        a feed showing Misc-or-Adult must never be answered from an entry
        belonging to Misc alone.
        """
        monkeypatch.setattr(reddit, "_cache", {})
        keys = []
        real_cached = reddit._cached

        def spy_cached(key, fetch):
            keys.append(key)
            return real_cached(key, fetch)

        monkeypatch.setattr(reddit, "_cached", spy_cached)
        monkeypatch.setattr(reddit, "_query_one", lambda params, retries=None: [])

        reddit.get_feed(self._or_feed())
        reddit.get_feed(
            {
                "title": "Action",
                "url": "http://www.reddit.com/r/fullmoviesonyoutube/search.json"
                "?q=flair%3AAction",
            }
        )

        assert keys[0] != keys[1], "a two-flair feed shares a cache entry"
        assert dict(keys[0])["link_flair_text"] == ("Misc", "Adult"), keys[0]


class TestRedditTop:
    def test_top_ranks_recent_posts_by_score(self, monkeypatch):
        """
        The archive cannot rank by score, so top() asks it for a recent window
        and sorts that locally. Without that, a Top feed is just Hot again, or
        an empty approximation of Reddit's real top listing.
        """
        captured = {}

        def fake_query(params, retries=None):
            captured.update(params)
            return [
                _post("low", 5),
                _post("high", 500),
                _post("middle", 50),
            ]

        monkeypatch.setattr(reddit, "_query", fake_query)
        monkeypatch.setattr(reddit, "_cached", lambda key, fetch: fetch())

        results = reddit.top("trailers", days=7).to_list()

        assert [item.title for item in results] == ["high", "middle", "low"]
        assert captured["subreddit"] == "trailers"
        assert captured["sort"] == "desc"
        assert captured["limit"] > reddit._LIMIT
        assert captured["after"]

    def test_top_keeps_only_the_display_limit(self, monkeypatch):
        posts = [_post("post%d" % n, n) for n in range(reddit._LIMIT + 10)]
        monkeypatch.setattr(reddit, "_query", lambda params, retries=None: posts)
        monkeypatch.setattr(reddit, "_cached", lambda key, fetch: fetch())

        assert len(reddit.top("trailers", days=7).to_list()) == reddit._LIMIT


class TestTheRetryBudgetIsHonoured:
    """
    _MAX_SECONDS is a ceiling on how long a person waits, so it has to bound the
    whole call rather than the gaps between attempts.

    It did not. The deadline was only checked between attempts and each attempt
    was allowed the full _TIMEOUT, so two slow attempts came to about 32 seconds
    through a budget written as 25 -- and measured on the Pi, a narrowed feed
    took 31.8 seconds before saying nothing.

    The waits were escalating too: 2, 4, 6, 8 seconds. That is twenty of the
    twenty-five second budget asleep, spent on a delay that buys nothing when the
    archive clears in about a second and a half.
    """

    class _Clock:
        """
        A monotonic clock the test drives, so time passing is an assertion
        rather than a real sleep.
        """

        def __init__(self):
            self.now = 1000.0
            self.slept = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.slept.append(seconds)
            self.now += seconds

    def _install(self, monkeypatch, clock):
        monkeypatch.setattr(reddit.time, "monotonic", clock.monotonic)
        monkeypatch.setattr(reddit.time, "sleep", clock.sleep)

    def test_the_waits_do_not_grow(self, monkeypatch):
        """
        Flat, and short. An escalating wait turned a recoverable blip into half a
        minute of nothing happening, and a burst of feed-clicks into a queue of
        each other's sleeps -- every feed is a distinct query, so nothing is
        served from cache to save the wait.
        """
        clock = self._Clock()
        self._install(monkeypatch, clock)
        calls = []

        def always_422(*args, **kwargs):
            calls.append(1)
            response = m.Mock(status_code=422)
            return response

        monkeypatch.setattr(reddit.requests, "get", always_422)

        with pytest.raises(reddit.ArchiveBusy):
            reddit._query_one({"title": "gay"}, retries=reddit._SEARCH_RETRIES)

        assert len(clock.slept) == len(calls) - 1
        assert clock.slept, "nothing was retried"
        assert set(clock.slept) == {reddit._RETRY_WAIT}, clock.slept
        assert reddit._RETRY_WAIT == 1

    def test_the_whole_call_fits_inside_the_budget(self, monkeypatch):
        """
        Five fast failures and the waits between them, which is the measured
        shape of the archive being busy: about a second and a half per attempt.
        """
        clock = self._Clock()
        self._install(monkeypatch, clock)

        def slow_422(*args, **kwargs):
            clock.now += 1.3
            return m.Mock(status_code=422)

        monkeypatch.setattr(reddit.requests, "get", slow_422)

        started = clock.monotonic()
        with pytest.raises(reddit.ArchiveBusy):
            reddit._query_one({"title": "gay"}, retries=reddit._SEARCH_RETRIES)
        elapsed = clock.monotonic() - started

        assert elapsed <= reddit._MAX_SECONDS, elapsed

    def test_an_attempt_is_not_given_more_than_the_budget_left(self, monkeypatch):
        """
        This is the one that was measured at 31.8s. Each attempt used to be
        allowed the full _TIMEOUT regardless of what was left, so a slow archive
        spent the whole _TIMEOUT twice.
        """
        clock = self._Clock()
        self._install(monkeypatch, clock)
        timeouts = []

        def slow_timeout(*args, **kwargs):
            timeouts.append(kwargs["timeout"])
            clock.now += kwargs["timeout"]  # the whole allowance is consumed
            raise reddit.requests.ReadTimeout("slow")

        monkeypatch.setattr(reddit.requests, "get", slow_timeout)

        with pytest.raises(reddit.ArchiveBusy):
            reddit._query_one({"title": "gay"}, retries=reddit._SEARCH_RETRIES)

        assert timeouts, "nothing was attempted"
        assert max(timeouts) <= reddit._MAX_SECONDS
        # Shrinking, never growing: no attempt is given more than the one before.
        assert timeouts == sorted(timeouts, reverse=True), timeouts

    def test_a_sleep_is_never_larger_than_the_time_left(self, monkeypatch):
        """
        The wait used to run to completion even when it began near the end of the
        budget, so a 422 arriving in the last moment still cost its full sleep and
        pushed the caller past the ceiling.
        """
        clock = self._Clock()
        self._install(monkeypatch, clock)

        def nearly_there(*args, **kwargs):
            # Burn almost all of the budget, then be busy.
            clock.now += reddit._MAX_SECONDS - 0.1
            return m.Mock(status_code=422)

        monkeypatch.setattr(reddit.requests, "get", nearly_there)

        with pytest.raises(reddit.ArchiveBusy):
            reddit._query_one({"title": "gay"}, retries=reddit._SEARCH_RETRIES)

        assert sum(clock.slept) <= reddit._MAX_SECONDS, clock.slept
        assert max(clock.slept) <= reddit._RETRY_WAIT

    def test_a_success_on_a_later_attempt_is_still_a_success(self, monkeypatch):
        """
        The point of retrying at all: the archive's own overload clears in
        seconds and the identical query works moments later.
        """
        clock = self._Clock()
        self._install(monkeypatch, clock)
        calls = []

        def busy_then_ok(*args, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                return m.Mock(status_code=422)
            return m.Mock(status_code=200, json=lambda: {"data": [_post("a post", 1)]})

        monkeypatch.setattr(reddit.requests, "get", busy_then_ok)

        rows = reddit._query_one({"title": "gay"}, retries=reddit._SEARCH_RETRIES)

        assert len(rows) == 1
        assert len(calls) == 3

    def test_the_ceiling_leaves_room_for_the_retry_budget(self, monkeypatch):
        """
        Pinned as a relationship rather than a number: the ceiling has to be long
        enough that the fast-failing case -- the only one worth retrying -- can
        actually use all of its attempts, or the retry count is a fiction.

        Five attempts at the measured 1.3s plus the waits between them is about
        eleven seconds, so a fifteen second ceiling runs the full budget.
        """
        attempts = reddit._SEARCH_RETRIES
        per_attempt = 1.3
        waits = (attempts - 1) * reddit._RETRY_WAIT
        assert attempts * per_attempt + waits <= reddit._MAX_SECONDS


class TestABusyArchiveIsNeitherEmptyNorSticky:
    """
    A busy archive must not look like an empty category, and must not stay that
    way.

    This is the bug that emptied "Gay & Lesbian" on r/Shortfilms. The archive
    timed out, all five attempts came back 422, and the helper returned an empty
    listing -- which the cache then stored for its full five minutes:

        20:34:15  archive timed out, five attempts, feed empty
        20:36:25  same feed, still empty, and the archive was never asked again

    Two separate faults, and both are pinned here. The empty listing was cached,
    so one unlucky moment held the feed down for the rest of the cache's life.
    And the empty listing was indistinguishable from a category with no posts, so
    the interface said "There are no items for this feed." about a feed that has
    always had films in it, and the only evidence was a line in a log nobody
    opens. A failure is now raised as ArchiveBusy so it can travel to the
    interface as a message, and it is not cached so the next click tries again.
    """

    def _feed(self):
        return {
            "title": "Gay & Lesbian",
            "url": "http://www.reddit.com/r/Shortfilms/search.json?q=gay",
        }

    def _flaky(self, monkeypatch, failures):
        """
        An archive that answers on attempt `failures` and is busy before that.
        """
        monkeypatch.setattr(reddit, "_cache", {})
        attempts = []

        def query(params, retries=None):
            attempts.append(1)
            if len(attempts) <= failures:
                raise reddit.ArchiveBusy(
                    "The Reddit archive is busy and did not answer."
                )
            return [_post("a post", 1)]

        monkeypatch.setattr(reddit, "_query", query)
        return attempts

    def test_a_failure_is_raised_rather_than_returned_as_an_empty_feed(
        self, monkeypatch
    ):
        """
        The distinction the interface needs. An empty list here is what made a
        busy archive and an empty category look the same on screen.
        """
        self._flaky(monkeypatch, failures=1)

        with pytest.raises(reddit.ArchiveBusy):
            reddit.get_feed(self._feed())

    def test_the_message_says_the_archive_is_busy(self, monkeypatch):
        """
        Driven through _query_one so it is the real message being checked rather
        than one written by the test.

        It reaches a person, so it has to say something to a person: what the
        cause is, and that it is not this server's fault -- which is the thing
        the reader cannot see and would otherwise go and check the television
        for. What it must not say is "Process exit" or a stack trace.
        """
        # No clock: the archive here never takes any time, and the only pause is
        # stubbed out, so the whole thing costs nothing in real seconds.
        monkeypatch.setattr(reddit.time, "sleep", lambda seconds: None)
        monkeypatch.setattr(
            reddit.requests, "get", lambda *a, **k: m.Mock(status_code=422)
        )

        with pytest.raises(reddit.ArchiveBusy) as caught:
            reddit._query_one({"title": "gay"}, retries=2)

        message = str(caught.value)
        assert "archive is busy" in message
        assert "not a fault of this server" in message
        assert "Traceback" not in message

    def test_a_failure_does_not_stick_in_the_cache(self, monkeypatch):
        attempts = self._flaky(monkeypatch, failures=1)

        with pytest.raises(reddit.ArchiveBusy):
            reddit.get_feed(self._feed())

        # The second click must reach the archive again, not a cached empty list.
        second = reddit.get_feed(self._feed())

        assert len(attempts) == 2, "the second click was answered from cache"
        assert len(second.to_list()) == 1

    def test_a_genuinely_empty_result_is_cached(self, monkeypatch):
        """
        The other side of it: the archive answering "no such posts" is an answer,
        not a failure, and caching that is the point of the cache -- it is what
        stops every click being a request to a volunteer-run archive.

        An empty list here must not raise, or a category with nothing in it would
        show an error instead of saying so.
        """
        monkeypatch.setattr(reddit, "_cache", {})
        attempts = []

        def nothing(params, retries=None):
            attempts.append(1)
            return []

        monkeypatch.setattr(reddit, "_query", nothing)

        assert len(reddit.get_feed(self._feed()).to_list()) == 0
        assert len(reddit.get_feed(self._feed()).to_list()) == 0

        assert len(attempts) == 1, "a real empty answer was asked for twice"


class TestABusyArchiveReachesTheInterfaceAsAMessage:
    """
    The last link in the chain: a busy archive has to arrive at the screen as
    something a person reads.

    Before this, a failed read came back as an empty list and the interface said
    "There are no items for this feed." The archive being busy was
    indistinguishable from the category being empty, and there was nothing to
    tell anyone the difference.
    """

    def _install_channel(self, monkeypatch, feed):
        """
        Install one channel whose every read goes to the archive, so a busy
        archive is reached through the same path a real one would be.
        """
        import lib.api.channels as api_channels

        class Channel:
            def getFeed(self, idx):
                return reddit.get_feed(feed)

            def search(self, q):
                return reddit.search("Shortfilms", q)

        class Installed:
            def getChannel(self, chid):
                return Channel()

        monkeypatch.setattr(api_channels, "_installed", Installed())
        return api_channels

    def test_a_busy_feed_becomes_a_service_unavailable_api_error(self, monkeypatch):
        from lib.api.common import ApiError

        api_channels = self._install_channel(
            monkeypatch,
            {
                "title": "Gay & Lesbian",
                "url": "http://www.reddit.com/r/Shortfilms/search.json?q=gay",
            },
        )

        def busy(params, retries=None):
            raise reddit.ArchiveBusy("The Reddit archive is busy and did not answer.")

        monkeypatch.setattr(reddit, "_query", busy)

        with pytest.raises(ApiError) as caught:
            api_channels.feed(chid="bfch_r_shortfilms", idx=11)

        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE
        assert "archive is busy" in str(caught.value)

    def test_a_busy_search_becomes_the_same_error(self, monkeypatch):
        """
        Search within the channel too, not just the feed list -- it is the same
        request and the same failure.
        """
        from lib.api.common import ApiError

        api_channels = self._install_channel(
            monkeypatch,
            {
                "title": "x",
                "url": "http://www.reddit.com/r/Shortfilms.json",
            },
        )

        def busy(params, retries=None):
            raise reddit.ArchiveBusy("The Reddit archive is busy.")

        monkeypatch.setattr(reddit, "_query", busy)

        with pytest.raises(ApiError) as caught:
            api_channels.search(chid="bfch_r_shortfilms", q="anything")

        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE

    def test_an_empty_feed_is_not_an_error(self, monkeypatch):
        """
        The other half, and the reason the two had to be separated. A category
        with genuinely nothing in it must still say so in its own words rather
        than through the error page.
        """
        import lib.api.channels as api_channels

        api_channels = self._install_channel(
            monkeypatch,
            {
                "title": "x",
                "url": "http://www.reddit.com/r/Shortfilms.json",
            },
        )
        monkeypatch.setattr(reddit, "_query", lambda params, retries=None: [])

        # An empty PlayItemList, and no exception: the interface's own wording
        # for "nothing here" still applies, and it is the right one.
        assert len(api_channels.feed(chid="bfch_r_shortfilms", idx=0).to_list()) == 0

    def test_a_busy_archive_does_not_empty_a_search_of_every_channel(self):
        """
        Search across all channels catches this per channel and skips the one
        that failed. If it did not, one busy archive would fail the search of
        every other channel -- the opposite of the fault this fixes, where one
        quiet archive looked like one empty category.
        """
        import inspect

        import lib.api.channels as api_channels

        source = inspect.getsource(api_channels._search_thread)
        assert (
            "except Exception" in source
        ), "a busy archive would take the whole cross-channel search with it"


class TestALinkToAChannelIsNotAVideo:
    """
    Posts that link a YouTube channel rather than a video.

    yt-dlp answers a channel URL by walking that channel's whole video list:
    measured on the Pi, no output and no exit after 100 seconds. The player
    reads yt-dlp in a loop with no timeout, so such an item does not fail and
    does not start -- it sits there, and the only way out is pressing stop.

    This lives here rather than in the one channel that showed it most often
    because every Reddit channel goes through _extract(). A filter that lives in
    a channel is a filter the next channel forgets.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/@AlteredPixelz",
            "https://youtube.com/@AlteredPixelz?si=_H-We6MQOQBVfBc5",
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
            "https://www.youtube.com/c/SomeLegacyChannel",
            "https://www.youtube.com/user/SomeOldUser",
            "https://m.youtube.com/@AlteredPixelz",
            "https://music.youtube.com/@SomeChannel",
        ],
    )
    def test_a_channel_link_is_dropped(self, url):
        items = reddit._extract(reddit._listing([_post("live now", 1, url)]), False)
        assert items.to_list() == []

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtu.be/nGiozUzOSAI",
            "https://youtu.be/nGiozUzOSAI?is=Xd4Cn3-vbSw1fg6a",
            "https://www.youtube.com/watch?v=EZhQDSpdbZo&si=oy7nYAyHjLzsJh5P",
            "https://youtube.com/shorts/_22QFmnVZP0?feature=share",
            "https://www.youtube.com/embed/xDA2IJLuS4M",
            "https://youtube.com/playlist?list=PLcd7kGWsSpuBbCDaaTf5PYy31AbhxE2fy",
            "https://m.youtube.com/watch?v=8cD1QWTYbvU",
        ],
    )
    def test_a_video_link_is_kept_in_every_form_that_resolves(self, url):
        items = reddit._extract(reddit._listing([_post("a video", 1, url)]), False)
        assert [item.url for item in items.to_list()] == [url]

    def test_a_handle_that_is_not_a_channel_is_not_mistaken_for_one(self):
        """
        The pattern matches the path after youtube.com/, so a video id beginning
        with a letter and a dash cannot be read as a handle. A slash is what
        separates the host from the handle, and a video link never has one there.
        """
        items = reddit._extract(
            reddit._listing([_post("not a handle", 1, "https://www.youtube.com/watch?v=abc")]),
            False,
        )
        assert len(items.to_list()) == 1

    def test_the_dropped_rows_do_not_shift_the_others(self):
        items = reddit._extract(
            reddit._listing(
                [
                    _post("first", 1, "https://youtu.be/aaaaaaaaaaa"),
                    _post("a channel", 1, "https://www.youtube.com/@AlteredPixelz"),
                    _post("second", 1, "https://youtu.be/bbbbbbbbbbb"),
                ]
            ),
            False,
        )
        assert [item.url for item in items.to_list()] == [
            "https://youtu.be/aaaaaaaaaaa",
            "https://youtu.be/bbbbbbbbbbb",
        ]

    def test_a_row_with_no_url_is_dropped_rather_than_becoming_an_item(self):
        """
        An item with no URL cannot be played, and this is the one place where
        "cannot play" is decided. A decision that quietly passes through
        something it cannot handle is the shape of the bug it prevents.
        """
        post = _post("no url", 1, "https://youtu.be/aaaaaaaaaaa")
        post["url"] = ""
        items = reddit._extract(reddit._listing([post]), False)
        assert items.to_list() == []

    def test_it_applies_to_every_channel_not_only_the_one_that_showed_it(self):
        """
        The point of putting it here. /r/Shortfilms carries these too, at about
        the same rate as /r/youtubevideos, and a channel added tomorrow gets
        this by going through the same three lines every other one does.
        """
        for sr in ("videos", "trailers", "Shortfilms", "Documentaries"):
            items = reddit._extract(
                reddit._listing([_post("live", 1, "https://www.youtube.com/@Someone")]),
                False,
            )
            assert items.to_list() == [], sr


class TestTopIsRankedByScore:
    """
    The "top" feed, and the claim its name makes.

    It used to be a window of the subreddit in time order with no ranking at
    all, so "Top Rated" returned the newest posts and had the same first item as
    the "Hot" feed beside it on every channel. This is what replaced that.
    """

    def _feed(self, url):
        return {"url": url}

    def test_a_top_feed_is_recognised_however_the_channel_writes_it(self):
        """
        The channels write these several ways and they all mean one request:
        /r/videos/top.json, /r/videos/top.json?sort=top&t=all,
        /r/trailers/top.json?t=month.
        """
        for url in (
            "http://www.reddit.com/r/videos/top.json",
            "http://www.reddit.com/r/videos/top.json?sort=top&t=all",
            "http://www.reddit.com/r/trailers/top.json?t=month",
        ):
            assert reddit._TOP_LISTING.search(url), url

    @pytest.mark.parametrize(
        "url",
        [
            "http://www.reddit.com/r/videos.json",
            "http://www.reddit.com/r/videos/new.json",
            "http://www.reddit.com/r/videos/search.json?q=comedy",
            # A flair feed carrying sort=top in its query string is a filter,
            # not Reddit's top listing, and must not be ranked as one.
            "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Art%27&sort=top",
        ],
    )
    def test_other_listings_are_not_mistaken_for_top(self, url):
        assert not reddit._TOP_LISTING.search(url), url

    def test_the_highest_scoring_post_comes_first(self):
        posts = [
            _post("first", 10, "https://youtu.be/aaaaaaaaaaa"),
            _post("second", 9000, "https://youtu.be/bbbbbbbbbbb"),
            _post("third", 50, "https://youtu.be/ccccccccccc"),
        ]
        fetched = []

        def fake_query(params, retries=None):
            fetched.append(dict(params))
            return list(posts)

        original_query = reddit._query
        original_cache = reddit._cached
        reddit._query = fake_query
        reddit._cached = lambda key, fetch: fetch()
        try:
            items = reddit.get_feed(
                self._feed("http://www.reddit.com/r/videos/top.json")
            )
        finally:
            reddit._query = original_query
            reddit._cached = original_cache

        assert [item.title for item in items.to_list()] == [
            "second",
            "third",
            "first",
        ]
        assert len(fetched) == 1, "ranking is one request, not one per feed"

    def test_a_missing_score_ranks_last_rather_than_crashing(self):
        posts = [_post("no score", 0, "https://youtu.be/aaaaaaaaaaa")]
        posts[0].pop("score")
        posts.append(_post("has score", 7, "https://youtu.be/bbbbbbbbbbb"))

        original_query = reddit._query
        original_cache = reddit._cached
        reddit._query = lambda params, retries=None: list(posts)
        reddit._cached = lambda key, fetch: fetch()
        try:
            items = reddit.get_feed(
                self._feed("http://www.reddit.com/r/videos/top.json")
            )
        finally:
            reddit._query = original_query
            reddit._cached = original_cache

        assert [item.title for item in items.to_list()] == [
            "has score",
            "no score",
        ]

    def test_it_asks_for_more_than_a_screenful(self):
        """
        The archive will not sort the answer, so the top thirty of a window is
        only the top thirty if the thirty handed back were already the best
        thirty -- and what it hands back is newest-first.
        """
        seen = {}

        def fake_query(params, retries=None):
            seen.update(params)
            return []

        original_query = reddit._query
        original_cache = reddit._cached
        reddit._query = fake_query
        reddit._cached = lambda key, fetch: fetch()
        try:
            reddit.get_feed(self._feed("http://www.reddit.com/r/videos/top.json"))
        finally:
            reddit._query = original_query
            reddit._cached = original_cache

        assert seen["limit"] > reddit._LIMIT

    def test_the_feed_still_shows_a_screenful(self):
        posts = [
            _post(f"post {n}", n, "https://youtu.be/%011d" % n) for n in range(120)
        ]

        def fake_query(params, retries=None):
            return list(posts)

        original_query = reddit._query
        original_cache = reddit._cached
        reddit._query = fake_query
        reddit._cached = lambda key, fetch: fetch()
        try:
            items = reddit.get_feed(
                self._feed("http://www.reddit.com/r/videos/top.json")
            )
        finally:
            reddit._query = original_query
            reddit._cached = original_cache

        assert len(items.to_list()) == reddit._LIMIT


class TestTheSortsThatAreNotShipped:
    """
    Reddit offers five sorts. Two of them are served; the other three are not,
    and this is why they are not.

    A feed named "Hot", "Best" or "Rising" that is time order is worse than no
    such feed: it answers, it looks right, and it is a ranking the user was not
    given. The archive's whole vocabulary is:

        'sort' must be one of asc, desc
    """

    def test_the_archive_has_no_ranking_parameter_to_ask_for(self):
        """
        Recorded as a fact about the archive rather than tested against it --
        this is a statement about a third party, and the test that matters is
        the one above: that what is served is ranked honestly.
        """
        assert reddit._API
        source = open(reddit.__file__).read()
        assert "asc, desc" in source or "sort=desc" in source

    @pytest.mark.parametrize("sort", ["hot", "best", "rising"])
    def test_a_sort_that_cannot_be_honest_is_not_served(self, sort):
        """
        Refused rather than answered with something else.

        "hot" and "rising" used to be recognised here and mapped onto the same
        newest-first listing as everything else, so a channel writing one would
        have got time order under a label promising a ranking and nothing would
        have said so. "best" was refused. All three are refused now, which is
        the only version of this where the helper's vocabulary and what it can
        honestly produce are the same thing.
        """
        assert reddit._feed_params(f"http://www.reddit.com/r/videos/{sort}.json") is None

    def test_the_channel_shows_nothing_rather_than_the_wrong_thing(self):
        """
        Which is the same path the retired sitewide domain feed takes: an empty
        listing, not an error. A channel asking for a ranking it cannot have
        gets an empty category, not a category of something else.
        """
        items = reddit.get_feed(
            {"url": "http://www.reddit.com/r/videos/best.json"}
        )
        assert items.to_list() == []

    def test_top_is_the_sort_that_is_served(self):
        params = reddit._feed_params("http://www.reddit.com/r/videos/top.json")
        assert params["after"] == reddit._top_window_date()

    def test_the_two_served_sorts_are_all_a_channel_can_write(self):
        """
        Across every Reddit channel in the tree: New and Top, and nothing
        naming a sort that cannot be produced. This is what keeps the refusals
        above from being theoretical.
        """
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "chls"
        listings = set()
        for feedlist in root.glob("bfch_*"):
            url_file = feedlist / "__init__.py"
            if not url_file.is_file():
                continue
            for line in url_file.read_text().splitlines():
                for sort in ("hot", "best", "rising"):
                    if f"/{sort}.json" in line:
                        listings.add((feedlist.name, sort))
        assert listings == set()

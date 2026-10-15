"""
The Reddit-backed channels.

lib.chanutils.reddit talks to Arctic Shift rather than Reddit's own API, so a
lot of what these channels do is translating a Reddit-style listing into
archive query parameters and then turning the archive's rows back into play
items. That translation is pure and worth pinning down; a change to it silently
empties a channel rather than raising.
"""

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

    def test_new_listing_is_oldest_first(self):
        """
        The old New feed was useless because the archive sorts by time and
        "asc" gives the oldest posts in the archive, not the newest.
        """
        params = reddit._feed_params("http://www.reddit.com/r/trailers/new.json")
        assert params["sort"] == "asc"

    def test_top_listing_uses_a_recent_window(self):
        params = reddit._feed_params("http://www.reddit.com/r/trailers/top.json")
        assert params["after"] == reddit._TOP_WINDOW

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


class TestAFailedReadingIsNotCached:
    """
    A busy archive must not look like an empty category, and must not stay that
    way.

    This is the bug that emptied "Gay & Lesbian" on r/Shortfilms. The archive
    timed out, the five attempts all came back 422, and the helper returned an
    empty listing -- which the cache then stored for its full five minutes:

        20:34:15  archive timed out, five attempts, feed empty
        20:36:25  same feed, still empty, and the archive was never asked again

    The second request was answered from cache. The archive had recovered by
    then, so the feed stayed empty for a category that has always had posts in
    it, and nothing in the interface said the difference between "no such posts"
    and "could not ask".
    """

    def _feed(self):
        return {
            "title": "Gay & Lesbian",
            "url": "http://www.reddit.com/r/Shortfilms/search.json?q=gay",
        }

    def test_a_failure_does_not_stick_in_the_cache(self, monkeypatch):
        monkeypatch.setattr(reddit, "_cache", {})

        attempts = []

        def flaky(params, retries=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise Exception("arctic shift: could not be read")
            return [_post("a post", 1)]

        monkeypatch.setattr(reddit, "_query", flaky)

        first = reddit.get_feed(self._feed())
        assert len(first.to_list()) == 0, "the first attempt is meant to fail"

        second = reddit.get_feed(self._feed())

        assert len(attempts) == 2, "the second click was answered from cache"
        assert len(second.to_list()) == 1, "the archive was never asked again"

    def test_a_genuinely_empty_result_is_cached(self, monkeypatch):
        """
        The other side of it: the archive answering "no such posts" is an answer,
        not a failure, and caching that is the point of the cache -- it is what
        stops every click being a request to a volunteer-run archive.
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

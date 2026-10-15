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

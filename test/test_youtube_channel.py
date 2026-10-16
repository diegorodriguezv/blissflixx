"""
The YouTube channel.

Almost nothing here is logic: the channel is a subreddit's posts, and what can
be played out of those is decided in lib.chanutils.reddit, which every Reddit
channel shares. What is worth pinning is that this channel is wired to the
subreddit it says it is, and that it does not promise a ranking the subreddit
cannot supply.
"""

import pytest

import bfch_youtube


class TestTheFeeds:
    def test_two_feeds_and_neither_of_them_a_rating(self):
        """
        New and Top, the two sorts the archive can honestly be asked for.

        There is no Hot, Best or Rising here or in any other Reddit channel:
        those are Reddit's own ranking functions and the archive computes
        none of them, so a feed wearing those names would be time order under
        a label promising a ranking. See lib.chanutils.reddit.

        Top is kept even though it cannot mean much in this subreddit -- see
        the test below -- because it is a real ranking of whatever scores are
        there, and dropping it here while shipping it elsewhere would be a
        statement about this channel that is not true of it.
        """
        titles = [feed["title"] for feed in bfch_youtube.feedlist()]
        assert titles == ["New", "Top"]

    def test_the_two_feeds_are_the_two_the_helper_serves(self):
        from lib.chanutils import reddit

        for feed in bfch_youtube.feedlist():
            assert reddit._feed_params(feed["url"]) is not None

    def test_the_feeds_name_the_subreddit_they_are_asking_for(self):
        for feed in bfch_youtube.feedlist():
            assert "/r/youtubevideos" in feed["url"]

    def test_the_urls_are_listings_the_reddit_helper_recognises(self):
        """
        A URL the helper cannot translate comes back as an empty list rather
        than an error, which is a channel that is silently empty -- and the
        helper says so for the retired sitewide domain feed, which is the shape
        this channel used to have.
        """
        from lib.chanutils import reddit

        for feed in bfch_youtube.feedlist():
            params = reddit._feed_params(feed["url"])
            assert params is not None, feed["url"]
            assert params["subreddit"] == "youtubevideos"

    def test_it_does_not_ask_for_a_sitewide_listing_anywhere(self):
        """
        The feed this channel was retired for. The archive cannot serve it, and
        a channel carrying it would show an empty category rather than say why.
        """
        from lib.chanutils import reddit

        for feed in bfch_youtube.feedlist():
            assert reddit._feed_params(feed["url"]) is not None


class TestTheChannelAnswers:
    def test_feed_passes_the_listing_through_untouched(self):
        """
        No filter in the channel. One that filters here is one the next Reddit
        channel does not have, and the items that need dropping are dropped in
        the helper all of them already go through.
        """
        import inspect

        source = inspect.getsource(bfch_youtube.feed)
        assert "_only_videos" not in source
        assert "_CHANNEL_URL" not in source

        seen = {}

        def fake_get_feed(entry):
            seen["entry"] = entry
            return "the listing, as returned"

        original = bfch_youtube.get_feed
        bfch_youtube.get_feed = fake_get_feed
        try:
            assert bfch_youtube.feed(0) == "the listing, as returned"
        finally:
            bfch_youtube.get_feed = original

        assert seen["entry"] is bfch_youtube.feedlist()[0]

    def test_search_asks_the_archive_about_this_subreddit(self):
        seen = {}

        def fake_search(subreddit, q):
            seen.update(subreddit=subreddit, q=q)
            return "the results"

        original = bfch_youtube.reddit_search
        bfch_youtube.reddit_search = fake_search
        try:
            assert bfch_youtube.search("kayak") == "the results"
        finally:
            bfch_youtube.reddit_search = original

        assert seen == {"subreddit": "youtubevideos", "q": "kayak"}


class TestTheChannelDescribesItself:
    def test_it_says_where_the_posts_come_from(self):
        """
        The channel is not YouTube's own listings and should not imply it is.
        Every Reddit channel names its subreddit in the description.
        """
        described = bfch_youtube.description()
        assert "/r/youtubevideos" in described
        assert "youtube.com" not in described.replace(
            "http://www.reddit.com/r/youtubevideos", ""
        )

    def test_it_is_named_apart_from_the_movies_channel(self):
        """
        There is also a "Youtube Movies" channel. Two entries differing only by
        one word are indistinguishable in a list of icons.
        """
        from bfch_r_fullmoviesonyoutube import name as movies_name

        assert bfch_youtube.name() != movies_name()


class TestWhatItDoesNotCarry:
    def test_it_carries_no_api_key(self):
        """
        The channel this replaces asked YouTube's Data API with a key written
        into the file -- a credential belonging to whoever published it,
        committed to a public repository. Nothing here needs one.
        """
        import inspect

        source = inspect.getsource(bfch_youtube)
        assert "googleapis.com" not in source
        assert "AIza" not in source

    @pytest.mark.parametrize("name", ["_YT_API_KEY", "_SEARCH_URL", "_INFO_URL"])
    def test_none_of_the_old_names_are_back(self, name):
        assert not hasattr(bfch_youtube, name)
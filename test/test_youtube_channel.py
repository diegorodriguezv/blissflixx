"""
The YouTube channel.

The channel is one subreddit's posts, and almost all of the logic is a filter:
r/youtubevideos carries links to channels as well as to videos, and a channel
link is not a thing this player can start. The rest is pinned here because the
failure it prevents is invisible -- the item simply never plays.
"""

import pytest

import bfch_youtube
from lib.playitem import PlayItem, PlayItemList


def _items(*urls):
    listing = PlayItemList()
    for url in urls:
        listing.add(PlayItem("title", None, url))
    return listing


class TestTheFeeds:
    def test_two_feeds_and_neither_of_them_a_rating(self):
        """
        r/youtubevideos does not get upvoted. Measured back to 2024, every one
        of a hundred posts scores 1, so every one is tied.

        A "Top Rated" feed here would rank a constant: the archive sorts by time
        and the helper re-sorts by score, which is all ones, so the feed would
        look like it worked and mean nothing. It is left out rather than
        included as an approximation.
        """
        titles = [feed["title"] for feed in bfch_youtube.feedlist()]
        assert titles == ["Hot", "New"]

    def test_the_feeds_name_the_subreddit_they_are_asking_for(self):
        for feed in bfch_youtube.feedlist():
            assert "/r/youtubevideos" in feed["url"]

    def test_the_urls_are_listings_the_reddit_helper_recognises(self):
        """
        A URL the helper cannot translate comes back as an empty list rather
        than an error, which is a channel that is silently empty.
        """
        from lib.chanutils import reddit

        for feed in bfch_youtube.feedlist():
            params = reddit._feed_params(feed["url"])
            assert params is not None, feed["url"]
            assert params["subreddit"] == "youtubevideos"


class TestOnlyVideos:
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
    def test_a_link_to_a_video_is_kept(self, url):
        kept = bfch_youtube._only_videos(_items(url))
        assert [item.url for item in kept.to_list()] == [url]

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/@AlteredPixelz",
            "https://youtube.com/@AlteredPixelz?si=_H-We6MQOQBVfBc5",
            "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
            "https://www.youtube.com/c/SomeLegacyChannel",
            "https://www.youtube.com/user/SomeOldUser",
            "https://m.youtube.com/@AlteredPixelz",
        ],
    )
    def test_a_link_to_a_channel_is_dropped(self, url):
        """
        yt-dlp answers a channel URL by walking the channel's whole video list.
        Measured on the Pi: no output and no exit after 100 seconds.

        The player reads that output in a loop with no timeout, so an item like
        this does not fail and does not start -- it sits there, and the only way
        out is to press stop. Three posts in a hundred in this subreddit are
        one of these, so it is the common case and not the exotic one.
        """
        kept = bfch_youtube._only_videos(_items(url))
        assert kept.to_list() == []

    def test_the_dropped_rows_do_not_shift_the_others(self):
        kept = bfch_youtube._only_videos(
            _items(
                "https://youtu.be/aaaaaaaaaaa",
                "https://www.youtube.com/@AlteredPixelz",
                "https://youtu.be/bbbbbbbbbbb",
            )
        )
        assert [item.url for item in kept.to_list()] == [
            "https://youtu.be/aaaaaaaaaaa",
            "https://youtu.be/bbbbbbbbbbb",
        ]

    def test_a_missing_url_is_dropped_rather_than_crashing(self):
        listing = PlayItemList()
        listing.add(PlayItem("title", None, None))
        assert bfch_youtube._only_videos(listing).to_list() == []


class TestTheChannelAnswers:
    def test_feed_runs_the_listing_through_the_filter(self):
        called = {}

        def fake_get_feed(entry):
            called["entry"] = entry
            return _items(
                "https://youtu.be/aaaaaaaaaaa", "https://www.youtube.com/@Someone"
            )

        bfch_youtube.get_feed = fake_get_feed
        try:
            items = bfch_youtube.feed(1)
        finally:
            del bfch_youtube.get_feed

        assert called["entry"]["title"] == "New"
        assert [item.url for item in items.to_list()] == ["https://youtu.be/aaaaaaaaaaa"]

    def test_search_runs_the_listing_through_the_filter(self):
        called = {}

        def fake_search(subreddit, q):
            called["subreddit"] = subreddit
            called["q"] = q
            return _items(
                "https://youtu.be/aaaaaaaaaaa", "https://www.youtube.com/c/Someone"
            )

        bfch_youtube.reddit_search = fake_search
        try:
            items = bfch_youtube.search("kayak")
        finally:
            del bfch_youtube.reddit_search

        assert called == {"subreddit": "youtubevideos", "q": "kayak"}
        assert [item.url for item in items.to_list()] == ["https://youtu.be/aaaaaaaaaaa"]


class TestTheChannelDescribesItself:
    def test_it_says_where_the_posts_come_from(self):
        """
        The channel is not YouTube's own listings and should not imply it is.
        Every other Reddit channel names its subreddit in the description.
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
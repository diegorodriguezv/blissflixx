"""
The channel page and its block list.

A channel that hides rows has to say how many and why, above the results those
rows were hidden from: a feed that is short without a word about it reads as a
channel that lost its content.
"""

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _page():
    return (ROOT / "html" / "pages" / "channel.html").read_text()


def _store():
    return (ROOT / "html" / "js" / "ChannelStore.js").read_text()


def _showmore():
    return (ROOT / "html" / "pages" / "showmore.html").read_text()


class TestTheHeaderIsShared:
    """
    A channel's "more" page wears the same header as its channel page.

    It used to be a back button and a title, which read as a page belonging to
    no channel at all; the header is one tag now so both pages say the same
    thing.
    """

    def test_both_pages_use_the_header_tag(self):
        for page in (_page(), _showmore()):
            assert "<channelheader" in page

    def test_the_header_carries_the_search_box(self):
        tag = (ROOT / "html" / "tags" / "channelheader.html").read_text()
        assert 'name="f_query"' in tag
        assert "Utils.goRoute('channel', opts.channel.id, 's'" in tag

    def test_the_channel_page_no_longer_doubles_the_header(self):
        page = _page()
        assert "search-box" not in page
        assert "channel-logo" not in page

    def test_the_channel_page_passes_its_query_to_the_header(self):
        assert "<channelheader channel={ channel } query={ query }>" in _page()

    def test_the_more_page_points_its_tabs_at_the_channel(self):
        page = _showmore()
        assert "page={ '#channel/' + chid }" in page
        # The feed it came from when there is one, unlit otherwise.
        assert "active={ lastFeed }" in page

    def test_the_dropdown_prompts_when_no_feed_is_chosen(self):
        """
        The dropdown used to display its first option when nothing was
        active, so on a "more" page it said Latest while the page was not on
        Latest -- and picking Latest then did nothing, because the value the
        dropdown already had was the one being picked. A prompt option cannot
        be mistaken for a feed, and picking any feed works.
        """
        tag = (ROOT / "html" / "tags" / "feedlist.html").read_text()
        assert 'value="-1" disabled' in tag
        assert "noneChosen" in tag

    def test_the_channel_remembers_the_feed_it_showed(self):
        page = _page()
        store = _store()
        assert "channelStore.rememberFeed(self.chid, self.feed)" in page
        assert "self.rememberFeed" in store
        assert "self.lastFeed" in store

    def test_the_more_page_offers_the_feed_it_came_from(self):
        page = _showmore()
        assert "active={ lastFeed }" in page
        assert "channelStore.lastFeed[self.chid]" in page

    def test_the_tag_is_registered(self):
        index = (ROOT / "html" / "index.html").read_text()
        assert "tags/channelheader.html" in index


class TestTheBlockedLine:
    def test_it_sits_between_the_feeds_and_the_results(self):
        page = _page()
        assert page.index("<feedlist") < page.index("blocked-report")
        assert page.index("blocked-report") < page.index("<itemlist")

    def test_it_says_how_many_rows_were_hidden(self):
        page = _page()
        assert "blocked.hidden" in page
        assert "blocked.rows" in page
        assert "rows blocked" in page

    def test_it_names_every_word_and_its_count(self):
        page = _page()
        assert "each={ w, i in blocked.words }" in page
        assert "{ w.word }" in page
        assert "{ w.count }" in page

    def test_there_is_no_line_without_a_report(self):
        assert "show={ blocked }" in _page()

    def test_the_line_is_muted_rather_than_a_result(self):
        css = (ROOT / "html" / "css" / "layout.css").read_text()
        assert ".blocked-report" in css
        assert "#6d90b0" in css


class TestTheStoreCarriesTheReport:
    def test_the_report_is_cached_with_the_rows(self):
        store = _store()
        assert "self.getListing" in store
        assert "listing.items" in store
        assert "listing.blocked" in store

    def test_a_channel_feed_uses_the_listing(self):
        store = _store()
        feed = store[store.index("self.getFeed = function"):store.index("self.showMore")]
        assert "getListing" in feed
        assert "getResults" not in feed

    def test_a_channel_search_uses_the_listing(self):
        store = _store()
        search = store[store.index("self.searchChannel = function"):store.index("self.searchAll = function")]
        assert "getListing" in search

    def test_the_page_hands_the_report_to_the_line(self):
        page = _page()
        assert "function(err, items, blocked)" in page
        assert page.count("self.blocked = blocked") == 2

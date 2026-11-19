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

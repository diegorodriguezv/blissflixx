"""
The IPTV channel's rows.

iptv-org lists streams, not channels, so one channel is often many rows -- and
Caracol TV is thirty-five. The channel groups them into one row carrying every
mirror, orders them by what the box has seen, and says how the channel is doing
as a set of sources:

    12 sources · 1 healthy · 2 tested

Nothing is hidden. A channel only moves to the bottom when every source has
been tried and none is healthy; while untried ones remain it stays in the
middle, because another source may still work.

The journal is monkeypatched rather than written: these are the ordering and
wording rules, and test_playquality.py owns the file.
"""

import pathlib
import time

import bfch_iptv_org as iptv


def stream(
    chid,
    name,
    url,
    quality=None,
    labels=None,
    closed=None,
    country="CO",
    categories=None,
):
    info = {
        "id": chid,
        "name": name,
        "country": country,
        "closed": closed,
        "categories": categories or [],
    }
    return {
        "channel": chid,
        "url": url,
        "quality": quality,
        "labels": labels or [],
        "channel_info": info,
    }


def journal(monkeypatch, entries):
    monkeypatch.setattr(iptv.playquality, "summary", lambda url: entries.get(url))


def rows(data):
    return iptv._extract(data).to_list()


def urls(row):
    return row.sources


def mark(row):
    """The status span the row's subtitle carries, if any."""
    start = row.subtitle.find('<span class="iptv-status')
    if start < 0:
        return None, ""
    end = row.subtitle.find("</span>", start)
    span = row.subtitle[start : end + 7]
    classes = span.split('"')[1].split()
    kind = classes[1] if len(classes) > 1 else ""
    text = span[span.find(">") + 1 : span.find("</span>")]
    return kind, text


class TestOneRowPerChannel:
    def test_the_mirrors_become_one_row(self):
        data = [
            stream("caracol", "Caracol TV", "http://a/1", "720p"),
            stream("caracol", "Caracol TV", "http://a/2", "1080p"),
            stream("rcn", "Canal RCN", "http://b/1", "1080p"),
        ]
        result = rows(data)
        assert [row.title for row in result] == ["Caracol TV", "Canal RCN"]
        assert urls(result[0]) == ["http://a/2", "http://a/1"]
        assert result[0].url == "http://a/2"

    def test_the_best_quality_goes_first(self):
        data = [
            stream("c", "C", "http://a/576", "576p"),
            stream("c", "C", "http://a/720", "720p"),
            stream("c", "C", "http://a/1080", "1080p"),
        ]
        assert urls(rows(data)[0]) == ["http://a/1080", "http://a/720", "http://a/576"]

    def test_a_discouraged_label_comes_after_a_clean_one(self):
        data = [
            stream("c", "C", "http://a/geo", "1080p", ["Geo-blocked"]),
            stream("c", "C", "http://a/plain", "1080p"),
        ]
        assert urls(rows(data)[0]) == ["http://a/plain", "http://a/geo"]

    def test_the_row_says_up_to_the_best_quality(self):
        """
        The label is about the channel's sources, not the first one: a 720p
        source with a 1080p sibling is "up to 1080p".
        """
        data = [
            stream("c", "C", "http://a/720", "720p"),
            stream("c", "C", "http://a/1080", "1080p"),
        ]
        assert "quality: <b>up to 1080p</b>" in rows(data)[0].subtitle

    def test_a_channel_with_one_quality_names_it_plainly(self):
        data = [
            stream("c", "C", "http://a/1", "1080p"),
            stream("c", "C", "http://a/2", "1080p"),
        ]
        assert "quality: <b>1080p</b>" in rows(data)[0].subtitle
        assert "up to" not in rows(data)[0].subtitle

    def test_a_channel_without_quality_says_nothing_about_it(self):
        data = [stream("c", "C", "http://a/1")]
        assert "quality:" not in rows(data)[0].subtitle

    def test_every_mirror_is_carried(self):
        """
        The player tries until one works, so it needs the whole list. A cap
        here would mark a channel failed while working URLs sat untried.
        """
        data = [
            stream("c", "C", "http://a/%d" % i, "720p") for i in range(8)
        ]
        assert len(urls(rows(data)[0])) == 8

    def test_a_stream_without_a_channel_is_skipped(self):
        data = [
            {"channel": None, "url": "http://a", "channel_info": {"name": "X"}},
            stream("c", "C", "http://b"),
        ]
        assert [row.title for row in rows(data)] == ["C"]


class TestOrderingByWhatWorked:
    def test_the_best_quality_wins_over_a_lower_one_that_worked(self, monkeypatch):
        """
        The person asked to watch the channel, not the safest picture. A
        1080p URL that has not been tried goes before a 720p that worked.
        """
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/bad": {"bad_at": now - 600},
                "http://a/good": {"ok_at": now - 3600, "ok_seconds": 6},
            },
        )
        data = [
            stream("c", "C", "http://a/bad", "1080p"),
            stream("c", "C", "http://a/unknown", "1080p"),
            stream("c", "C", "http://a/good", "720p"),
        ]
        assert urls(rows(data)[0])[0] == "http://a/unknown"

    def test_a_failed_1080p_does_not_jump_a_working_720p(self, monkeypatch):
        """Bad ones are last whatever they claim to be."""
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/bad": {"bad_at": now - 60},
                "http://a/720": {"ok_at": now - 60, "ok_seconds": 6},
            },
        )
        data = [
            stream("c", "C", "http://a/bad", "1080p"),
            stream("c", "C", "http://a/720", "720p"),
        ]
        assert urls(rows(data)[0]) == ["http://a/720", "http://a/bad"]

    def test_within_one_quality_the_one_that_worked_goes_first(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {"http://a/good": {"ok_at": now - 60, "ok_seconds": 6}},
        )
        data = [
            stream("c", "C", "http://a/unknown", "1080p"),
            stream("c", "C", "http://a/good", "1080p"),
        ]
        assert urls(rows(data)[0]) == ["http://a/good", "http://a/unknown"]

    def test_a_working_channel_sorts_before_an_untested_one(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/good": {"ok_at": now - 60, "ok_seconds": 5}})
        data = [
            stream("unknown", "Unknown", "http://b"),
            stream("worked", "Worked", "http://a/good"),
        ]
        assert [row.title for row in rows(data)] == ["Worked", "Unknown"]

    def test_a_failed_channel_sorts_last(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/bad": {"bad_at": now - 60}})
        data = [
            stream("bad", "Failed", "http://a/bad"),
            stream("unknown", "Unknown", "http://b"),
        ]
        assert [row.title for row in rows(data)] == ["Unknown", "Failed"]

    def test_a_faster_working_channel_comes_first(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/slow": {"ok_at": now - 60, "ok_seconds": 14},
                "http://a/fast": {"ok_at": now - 60, "ok_seconds": 4},
            },
        )
        data = [
            stream("slow", "Slow", "http://a/slow"),
            stream("fast", "Fast", "http://a/fast"),
        ]
        assert [row.title for row in rows(data)] == ["Fast", "Slow"]

    def test_an_outcome_older_than_a_week_is_not_evidence(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {"http://a/old": {"ok_at": now - 8 * 24 * 3600, "ok_seconds": 5}},
        )
        data = [stream("old", "Old", "http://a/old")]
        kind, text = mark(rows(data)[0])
        assert kind == ""
        assert text == "1 source"


class TestTheCounts:
    def test_an_unplayed_channel_lists_its_sources(self, monkeypatch):
        journal(monkeypatch, {})
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert (kind, text) == ("", "1 source")

    def test_many_sources_untried(self, monkeypatch):
        journal(monkeypatch, {})
        data = [stream("c", "C", "http://a/%d" % i) for i in range(12)]
        kind, text = mark(rows(data)[0])
        assert (kind, text) == ("", "12 sources")

    def test_a_healthy_channel_counts_healthy_and_tested(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"ok_at": now - 60, "ok_seconds": 6}})
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "ok"
        assert text == "1 source · 1 healthy · 1 tested"

    def test_a_failure_counts_as_tested_not_healthy(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"bad_at": now - 60}})
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "bad"
        assert text == "1 source · 0 healthy · 1 tested"

    def test_a_freeze_counts_as_tested_not_healthy(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"froze_at": now - 60}})
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "bad"
        assert text == "1 source · 0 healthy · 1 tested"

    def test_a_partial_channel_is_amber_and_stays_in_the_middle(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/bad": {"bad_at": now - 60},
                "http://a/good": {"ok_at": now - 60, "ok_seconds": 5},
            },
        )
        data = [
            stream("c", "C", "http://a/bad"),
            stream("c", "C", "http://a/good"),
            stream("c", "C", "http://a/untried"),
        ]
        kind, text = mark(rows(data)[0])
        assert kind == "ok"
        assert text == "3 sources · 1 healthy · 2 tested"

    def test_only_failures_are_amber_until_every_source_is_tried(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"bad_at": now - 60}})
        data = [
            stream("c", "C", "http://a/one"),
            stream("c", "C", "http://a/two"),
        ]
        kind, text = mark(rows(data)[0])
        assert kind == "warn"
        assert text == "2 sources · 0 healthy · 1 tested"

    def test_every_source_failed_is_red(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/one": {"bad_at": now - 60},
                "http://a/two": {"froze_at": now - 60},
            },
        )
        data = [
            stream("c", "C", "http://a/one"),
            stream("c", "C", "http://a/two"),
        ]
        kind, text = mark(rows(data)[0])
        assert kind == "bad"
        assert text == "2 sources · 0 healthy · 2 tested"

    def test_a_closed_channel_is_judged_by_its_sources(self, monkeypatch):
        """
        "closed" is an upstream fact about the channel, not a play outcome; it
        no longer decides the row. With no evidence the row just says how many
        sources there are.
        """
        journal(monkeypatch, {})
        data = [stream("c", "C", "http://a/one", closed="2026-01-01")]
        kind, text = mark(rows(data)[0])
        assert (kind, text) == ("", "1 source")

    def test_a_later_success_clears_an_earlier_failure(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/one": {
                    "bad_at": now - 600,
                    "ok_at": now - 60,
                    "ok_seconds": 5,
                }
            },
        )
        kind, _text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "ok"

    def test_a_later_failure_clears_an_earlier_success(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/one": {
                    "ok_at": now - 600,
                    "bad_at": now - 60,
                    "ok_seconds": 5,
                }
            },
        )
        kind, _text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "bad"


class TestMovingToTheEnd:
    def test_only_a_channel_with_every_source_failed_goes_last(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/all": {"bad_at": now - 60},
                "http://p/one": {"bad_at": now - 60},
                "http://h/one": {"ok_at": now - 60, "ok_seconds": 5},
            },
        )
        data = [
            stream("partial", "Partial", "http://p/one"),
            stream("partial", "Partial", "http://p/two"),
            stream("all", "All failed", "http://a/all"),
            stream("healthy", "Healthy", "http://h/one"),
        ]
        assert [row.title for row in rows(data)] == ["Healthy", "Partial", "All failed"]

    def test_a_partial_channel_is_not_moved(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://p/one": {"bad_at": now - 60}})
        data = [
            stream("p", "Partial", "http://p/one"),
            stream("p", "Partial", "http://p/two"),
        ]
        # One source failed, one is untried: middle, not bottom.
        assert iptv._channel_rank(data) == (1, 0.0)


class TestTheFeeds:
    def test_the_page_starts_with_the_curated_list(self):
        """
        No network while the installed-channel list is built: this is the
        list that was always here, and the full one is asked for when the
        channel page opens.
        """
        feeds = iptv.feedlist()
        assert feeds[0]["title"] == "Colombia"
        assert feeds[12]["filter"] == {"language": "es"}
        assert feeds[0]["group"] == "Countries"
        assert feeds[12]["group"] == "Languages"

    def test_the_full_list_adds_countries_and_categories(self, monkeypatch):
        countries = [
            {"name": "Colombia", "code": "CO"},
            {"name": "Austria", "code": "AT"},
            {"name": "Zimbabwe", "code": "ZW"},
        ]
        categories = [
            {"id": "news", "name": "News"},
            {"id": "sports", "name": "Sports"},
            {"id": "xxx", "name": "Adult"},
        ]

        def fake_get(key, **kwargs):
            return {"countries": countries, "categories": categories}[key]

        monkeypatch.setattr(iptv, "_get_cached", fake_get)
        monkeypatch.setattr(iptv, "_CACHE", {})
        feeds = iptv.feedlist_full()
        # The curated indices are untouched, so an old route still works.
        assert feeds[0]["title"] == "Colombia"
        assert feeds[12]["filter"] == {"language": "es"}
        titles = [f["title"] for f in feeds]
        assert "Austria" in titles
        assert "News" in titles
        assert "Adult" not in titles
        assert [f for f in feeds if f["title"] == "Austria"][0]["group"] == "More countries"
        assert [f for f in feeds if f["title"] == "News"][0]["group"] == "Categories"

    def test_a_category_feed_keeps_only_that_category(self, monkeypatch):
        monkeypatch.setattr(
            iptv,
            "load_all_streams",
            lambda: [
                stream("n", "News One", "http://n", categories=["news"]),
                stream("s", "Sport One", "http://s", categories=["sports"]),
            ],
        )
        monkeypatch.setattr(
            iptv,
            "_feeds",
            lambda: [
                {
                    "title": "News",
                    "group": "Categories",
                    "filter": {"category": "news"},
                }
            ],
        )
        rows = iptv.feed(0).to_list()
        assert [row.title for row in rows] == ["News One"]

    def test_a_category_feed_can_be_empty(self, monkeypatch):
        monkeypatch.setattr(iptv, "load_all_streams", lambda: [])
        monkeypatch.setattr(
            iptv,
            "_feeds",
            lambda: [
                {"title": "News", "group": "Categories", "filter": {"category": "news"}}
            ],
        )
        assert iptv.feed(0).to_list() == []


class TestTheClientChain:
    """The row's mirrors have to survive the trip to the play request."""

    def test_the_row_hands_them_to_the_player(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "itemlist.html").read_text()
        assert "item.subs, item.sources" in tag

    def test_the_store_sends_them(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        store = (root / "html" / "js" / "PlayerStore.js").read_text()
        assert "args.sources = sources" in store

    def test_the_bar_can_say_which_source_is_being_tried(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        bar = (root / "html" / "tags" / "playbar.html").read_text()
        assert 'class="play-detail"' in bar
        assert "{ detail }" in bar
        assert "fa-refresh" in bar
        assert "retrying" in bar
        # The retry is the icon alone; the words are for the log, not the bar.
        assert "show={ !retrying }" in bar

    def test_the_iptv_feed_is_not_kept_for_a_minute(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        store = (root / "html" / "js" / "ChannelStore.js").read_text()
        assert "bfch_iptv_org' ? 0" in store
        assert "ttl !== 0" in store

    def test_the_channel_page_asks_for_the_full_feed_list(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "channel.html").read_text()
        store = (root / "html" / "js" / "ChannelStore.js").read_text()
        assert "channelStore.getFeeds" in page
        assert "channel.full_feeds" in page
        assert "self.getFeeds" in store
        assert "'feedlist'" in store

    def test_many_feeds_become_a_dropdown(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "feedlist.html").read_text()
        assert "<select" in tag
        assert "flat" in tag
        assert "__disabled" in tag
        assert "MANY" in tag

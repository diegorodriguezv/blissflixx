"""
The IPTV channel's rows.

iptv-org lists streams, not channels, so one channel is often many rows -- and
Caracol TV is thirty-five. The channel groups them into one row with the
mirrors behind it, orders the mirrors by what the box has seen, and says in
the row how the channel behaved here. Nothing is hidden: a channel that failed
is still there, at the bottom, with the age of the failure on it.

The journal is monkeypatched rather than written: these are the ordering and
wording rules, and test_playquality.py owns the file.
"""

import pathlib
import time

import bfch_iptv_org as iptv


def stream(chid, name, url, quality=None, labels=None, closed=None, country="CO"):
    info = {
        "id": chid,
        "name": name,
        "country": country,
        "closed": closed,
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
    kind = span.split('"')[1].split()[-1]
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

    def test_the_row_shows_the_chosen_streams_quality(self):
        data = [
            stream("c", "C", "http://a/720", "720p"),
            stream("c", "C", "http://a/1080", "1080p"),
        ]
        assert "1080p" in rows(data)[0].subtitle

    def test_mirrors_are_capped(self):
        data = [
            stream("c", "C", "http://a/%d" % i, "720p") for i in range(8)
        ]
        assert len(urls(rows(data)[0])) == iptv._MAX_SOURCES

    def test_a_stream_without_a_channel_is_skipped(self):
        data = [
            {"channel": None, "url": "http://a", "channel_info": {"name": "X"}},
            stream("c", "C", "http://b"),
        ]
        assert [row.title for row in rows(data)] == ["C"]


class TestOrderingByWhatWorked:
    def test_a_mirror_that_worked_goes_first(self, monkeypatch):
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
        assert urls(rows(data)[0])[0] == "http://a/good"

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
        kind, _text = mark(rows(data)[0])
        assert kind is None


class TestTheMark:
    def test_a_worked_channel_says_so_with_the_time_and_age(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {"http://a/one": {"ok_at": now - 7200, "ok_seconds": 6}},
        )
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "ok"
        assert text.startswith("worked · 6s · 2h ago")

    def test_a_slow_start_is_called_slow(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {"http://a/one": {"ok_at": now - 60, "ok_seconds": 14}},
        )
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "warn"
        assert text.startswith("slow · 14s")

    def test_a_failure_says_when(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"bad_at": now - 600}})
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "bad"
        assert text == "failed · 10m ago"

    def test_a_freeze_says_so(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"froze_at": now - 3600}})
        kind, text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind == "warn"
        assert text == "froze · 1h ago"

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

    def test_any_working_mirror_is_a_working_channel(self, monkeypatch):
        now = time.time()
        journal(
            monkeypatch,
            {
                "http://a/bad": {"bad_at": now - 60},
                "http://a/good": {"ok_at": now - 3600, "ok_seconds": 5},
            },
        )
        data = [
            stream("c", "C", "http://a/bad"),
            stream("c", "C", "http://a/good"),
        ]
        kind, _text = mark(rows(data)[0])
        assert kind == "ok"

    def test_a_closed_channel_with_no_evidence_says_closed(self, monkeypatch):
        journal(monkeypatch, {})
        data = [stream("c", "C", "http://a/one", closed="2026-01-01")]
        kind, text = mark(rows(data)[0])
        assert (kind, text) == ("bad", "closed")

    def test_a_worked_closed_channel_is_shown_as_worked(self, monkeypatch):
        now = time.time()
        journal(monkeypatch, {"http://a/one": {"ok_at": now - 60, "ok_seconds": 5}})
        data = [stream("c", "C", "http://a/one", closed="2026-01-01")]
        kind, _text = mark(rows(data)[0])
        assert kind == "ok"

    def test_an_unplayed_channel_has_no_mark(self, monkeypatch):
        journal(monkeypatch, {})
        kind, _text = mark(rows([stream("c", "C", "http://a/one")])[0])
        assert kind is None


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

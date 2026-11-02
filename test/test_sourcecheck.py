"""
The quick check of a channel's other sources.

Nothing here talks to the network: requests.get is replaced with a response
that says what the test needs. What is pinned is the verdict -- a playlist with
segments is reachable, an empty one is not, an error page is not -- and the
wait used when a source fails immediately.
"""

import threading
import time

import pytest

import lib.player.sourcecheck as sourcecheck


class Response:
    def __init__(self, status=200, content_type="application/vnd.apple.mpegurl", body=b""):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        self.body = body
        self.closed = False

    def iter_content(self, size):
        yield self.body

    def close(self):
        self.closed = True


def fake_get(responses):
    def get(url, **kwargs):
        found = responses[url]
        if isinstance(found, Exception):
            raise found
        if isinstance(found, float):
            time.sleep(found)
            return Response(body=b"#EXTM3U\n#EXTINF:4,\nseg.ts\n")
        return found

    return get


def check(monkeypatch, responses):
    monkeypatch.setattr(sourcecheck.requests, "get", fake_get(responses))
    return sourcecheck.SourceCheck(list(responses))


class TestTheVerdict:
    def test_a_playlist_with_segments_is_good(self, monkeypatch):
        c = check(
            monkeypatch,
            {"a": Response(body=b"#EXTM3U\n#EXTINF:4,\nseg.ts\n")},
        )
        c.wait(2)
        assert c.result("a") is True

    def test_a_master_playlist_is_good(self, monkeypatch):
        c = check(
            monkeypatch,
            {"a": Response(body=b"#EXTM3U\n#EXT-X-STREAM-INF:...\nchild.m3u8\n")},
        )
        c.wait(2)
        assert c.result("a") is True

    def test_an_empty_playlist_is_bad(self, monkeypatch):
        """What a "Not 24/7" channel serves when it is off the air."""
        c = check(
            monkeypatch,
            {"a": Response(body=b"#EXTM3U\n#EXT-X-VERSION:3\n")},
        )
        c.wait(2)
        assert c.result("a") is False

    def test_a_404_is_bad(self, monkeypatch):
        c = check(monkeypatch, {"a": Response(status=404, body=b"")})
        c.wait(2)
        assert c.result("a") is False

    def test_a_timeout_is_bad(self, monkeypatch):
        c = check(monkeypatch, {"a": RuntimeError("timed out")})
        c.wait(2)
        assert c.result("a") is False

    def test_an_error_page_is_bad(self, monkeypatch):
        c = check(
            monkeypatch,
            {"a": Response(content_type="text/html", body=b"<html>oops</html>")},
        )
        c.wait(2)
        assert c.result("a") is False

    def test_a_video_body_is_good(self, monkeypatch):
        c = check(
            monkeypatch,
            {"a": Response(content_type="video/mp4", body=b"\x00\x00\x00")},
        )
        c.wait(2)
        assert c.result("a") is True

    def test_a_source_still_in_flight_is_unknown(self, monkeypatch):
        started = threading.Event()
        release = threading.Event()

        def get(url, **kwargs):
            started.set()
            release.wait(2)
            return Response(body=b"#EXTM3U\n#EXTINF:4,\nseg.ts\n")

        monkeypatch.setattr(sourcecheck.requests, "get", get)
        c = sourcecheck.SourceCheck(["a"])
        started.wait(2)
        assert c.result("a") is None
        release.set()
        c.wait(2)
        assert c.result("a") is True


class TestWaiting:
    def test_the_wait_returns_as_soon_as_a_good_source_answers(self, monkeypatch):
        c = check(
            monkeypatch,
            {
                "slow": 0.5,
                "good": Response(body=b"#EXTM3U\n#EXTINF:4,\nseg.ts\n"),
            },
        )
        started = time.time()
        good = c.wait(2)
        assert good == ["good"]
        assert time.time() - started < 0.4

    def test_it_does_not_wait_for_the_full_time_when_everything_answered(
        self, monkeypatch
    ):
        c = check(
            monkeypatch,
            {
                "a": Response(status=404),
                "b": Response(body=b"#EXTM3U\n#EXT-X-VERSION:3\n"),
            },
        )
        started = time.time()
        assert c.wait(5) == []
        assert time.time() - started < 2

    def test_good_sources_are_fastest_first(self, monkeypatch):
        c = check(
            monkeypatch,
            {
                "slow": 0.2,
                "fast": Response(body=b"#EXTM3U\n#EXTINF:4,\nseg.ts\n"),
            },
        )
        deadline = time.time() + 2
        while not c.done() and time.time() < deadline:
            time.sleep(0.01)
        assert c.good() == ["fast", "slow"]

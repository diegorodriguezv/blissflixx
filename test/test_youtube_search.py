"""
lib.chanutils.youtube

Searching YouTube by asking YouTube, through yt-dlp. Everything here stubs the
subprocess: the parsing is what can go wrong, and the search itself is
exercised on the box (ten results in about five seconds, no key, no cookies).
"""

import json
import subprocess
import types

import pytest

from lib.chanutils import youtube


def result(**fields):
    base = {
        "id": "CLJDR-vFUdY",
        "title": "Silo — Season 2 Official Trailer | Apple TV",
        "duration": 148,
        "uploader": "Apple TV",
    }
    base.update(fields)
    return base


def run_with(monkeypatch, videos, returncode=0, stderr=b""):
    body = "".join(json.dumps(v) + "\n" for v in videos).encode("utf-8")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return types.SimpleNamespace(returncode=returncode, stdout=body, stderr=stderr)

    monkeypatch.setattr(youtube.subprocess, "run", fake_run)
    return seen


class TestSearch:
    def test_a_result_becomes_a_playable_row(self, monkeypatch):
        run_with(monkeypatch, [result()])
        items = youtube.search("silo trailer").to_dict()
        assert items[0]["title"] == "Silo — Season 2 Official Trailer | Apple TV"
        assert items[0]["url"] == "https://www.youtube.com/watch?v=CLJDR-vFUdY"
        assert items[0]["img"] == "https://i.ytimg.com/vi/CLJDR-vFUdY/mqdefault.jpg"
        assert items[0]["subtitle"] == "2:28 · Apple TV"

    def test_the_thumbnail_is_the_stable_one(self, monkeypatch):
        """
        The URLs in the results are signed and expire; the still for an id does
        not, so a row added to a playlist keeps its picture.
        """
        run_with(monkeypatch, [result()])
        assert "sqp=" not in youtube.search("x").to_dict()[0]["img"]

    def test_it_asks_for_the_limit_and_only_the_listing(self, monkeypatch):
        seen = run_with(monkeypatch, [result()])
        youtube.search("silo trailer", limit=5)
        cmd = seen["cmd"]
        assert "ytsearch5:silo trailer" in cmd
        assert "-j" in cmd and "--flat-playlist" in cmd
        assert "--ignore-config" in cmd

    def test_an_hour_long_video_reads_as_hours(self, monkeypatch):
        run_with(monkeypatch, [result(duration=3723)])
        assert youtube.search("x").to_dict()[0]["subtitle"].startswith("1:02:03")

    def test_a_live_video_has_no_duration_but_keeps_its_channel(self, monkeypatch):
        run_with(monkeypatch, [result(duration=None)])
        assert youtube.search("x").to_dict()[0]["subtitle"] == "Apple TV"

    def test_a_result_without_an_id_or_title_is_skipped(self, monkeypatch):
        run_with(
            monkeypatch,
            [result(id=None), result(title="  "), result(title="Kept")],
        )
        items = youtube.search("x").to_dict()
        assert [i["title"] for i in items] == ["Kept"]

    def test_an_empty_answer_is_an_error_not_an_empty_list(self, monkeypatch):
        """
        "No results" and "we could not ask" are different, and only one of them
        should look like a search that found nothing.
        """
        run_with(monkeypatch, [], stderr=b"ERROR: unable to reach youtube\n")
        with pytest.raises(RuntimeError, match="unable to reach youtube"):
            youtube.search("x")

    def test_a_timeout_says_so(self, monkeypatch):
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 60)

        monkeypatch.setattr(youtube.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="did not answer"):
            youtube.search("x")


class TestDuration:
    @pytest.mark.parametrize(
        "seconds,shown", [(5, "0:05"), (148, "2:28"), (3723, "1:02:03")]
    )
    def test_durations_read_the_way_videos_are_described(self, seconds, shown):
        assert youtube._duration(seconds) == shown

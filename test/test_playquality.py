"""
The play-quality journal.

What the box has seen of a stream is the only reliability evidence there is:
iptv-org publishes a resolution and some labels, not whether a URL is alive.
These tests pin the file's shape, its persistence, and the rule that a corrupt
or unreadable journal costs the mark and never the play.
"""

import json
import time

import lib.playquality as playquality


def _journal(tmp_path):
    return playquality.Journal(str(tmp_path / "play-quality.json"))


class TestWhatIsRecorded:
    def test_a_success_keeps_the_time_to_picture(self, tmp_path):
        journal = _journal(tmp_path)
        journal.record_ok("http://a/stream.m3u8", 6.42)
        entry = journal.summary("http://a/stream.m3u8")
        assert entry["ok_seconds"] == 6.4
        assert entry["ok_at"] <= time.time()

    def test_a_failure_keeps_why(self, tmp_path):
        journal = _journal(tmp_path)
        journal.record_failed("http://b/stream.m3u8", "Could not open the stream")
        assert (
            journal.summary("http://b/stream.m3u8")["bad_reason"]
            == "Could not open the stream"
        )

    def test_a_freeze_is_its_own_event(self, tmp_path):
        journal = _journal(tmp_path)
        journal.record_froze("http://c/stream.m3u8")
        entry = journal.summary("http://c/stream.m3u8")
        assert "froze_at" in entry
        assert "ok_at" not in entry

    def test_the_events_do_not_overwrite_each_other(self, tmp_path):
        """A mark needs the order: worked, then failed, is not "worked"."""
        journal = _journal(tmp_path)
        journal.record_ok("http://d/stream.m3u8", 4)
        time.sleep(0.01)
        journal.record_failed("http://d/stream.m3u8", "gone")
        entry = journal.summary("http://d/stream.m3u8")
        assert entry["ok_at"] < entry["bad_at"]
        assert entry["ok_seconds"] == 4.0

    def test_nothing_known_is_none(self, tmp_path):
        assert _journal(tmp_path).summary("http://unknown") is None


class TestTheFile:
    def test_it_survives_a_restart(self, tmp_path):
        first = _journal(tmp_path)
        first.record_ok("http://a/stream.m3u8", 5)
        second = _journal(tmp_path)
        assert second.summary("http://a/stream.m3u8")["ok_seconds"] == 5.0

    def test_a_corrupt_file_is_nothing_known(self, tmp_path):
        path = tmp_path / "play-quality.json"
        path.write_text("{not json")
        journal = playquality.Journal(str(path))
        assert journal.summary("http://a") is None
        journal.record_ok("http://a", 3)
        assert journal.summary("http://a")["ok_seconds"] == 3.0

    def test_a_missing_directory_is_made(self, tmp_path):
        journal = playquality.Journal(str(tmp_path / "deep" / "play-quality.json"))
        journal.record_ok("http://a", 2)
        assert (tmp_path / "deep" / "play-quality.json").exists()

    def test_an_unwritable_file_does_not_raise(self, tmp_path, monkeypatch):
        journal = _journal(tmp_path)

        def refuse(*args, **kwargs):
            raise OSError("read-only")

        monkeypatch.setattr(playquality.os, "replace", refuse)
        journal.record_ok("http://a", 2)
        assert journal.summary("http://a")["ok_seconds"] == 2.0

    def test_old_entries_are_dropped_on_save(self, tmp_path, monkeypatch):
        monkeypatch.setattr(playquality, "KEEP_SECONDS", 60)
        journal = _journal(tmp_path)
        journal.record_failed("http://old", "ancient")
        with open(journal.path) as fh:
            data = json.load(fh)
        data["http://old"]["bad_at"] = time.time() - 120
        with open(journal.path, "w") as fh:
            json.dump(data, fh)
        reopened = playquality.Journal(journal.path)
        reopened.record_ok("http://new", 1)
        assert reopened.summary("http://old") is None
        assert reopened.summary("http://new") is not None

    def test_the_file_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(playquality, "MAX_ENTRIES", 2)
        journal = _journal(tmp_path)
        for i in range(4):
            journal.record_ok("http://%d" % i, i)
            time.sleep(0.005)
        reopened = playquality.Journal(journal.path)
        assert reopened.summaries(["http://0", "http://1", "http://2", "http://3"]).keys() == {
            "http://2",
            "http://3",
        }


class TestWhatIsRead:
    def test_summaries_asks_only_for_what_was_asked(self, tmp_path):
        journal = _journal(tmp_path)
        journal.record_ok("http://a", 1)
        journal.record_ok("http://b", 2)
        assert set(journal.summaries(["http://a", "http://c"])) == {"http://a"}

    def test_a_summary_is_a_copy(self, tmp_path):
        journal = _journal(tmp_path)
        journal.record_ok("http://a", 1)
        entry = journal.summary("http://a")
        entry["ok_seconds"] = 999
        assert journal.summary("http://a")["ok_seconds"] == 1.0

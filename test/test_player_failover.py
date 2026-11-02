"""
A row with mirrors, and what the player does when one does not start.

The IPTV channel hands a channel's addresses to a play, best first. The player
tries the next one only when the picture never started: a stream that played
and then died is a different problem, and restarting it would throw away where
the person was. Each attempt is timed for the quality journal, and the count
the bar shows stays the person's wait, not the retry's.

No pipeline is started. The pipe is a stand-in with the fields the failover
reads, and _play is watched rather than run.
"""

import time

import pytest

import lib.player.player as player_module
import lib.playquality as playquality


class SourcePipe:
    def __init__(self, sources, index=0, media_at=None, track=True, subs=None):
        self.title = "A Channel"
        self.sources = sources
        self.source_index = index
        self.subs = subs
        self.track = track
        self.procs = [type("Stage", (), {"_media_at": media_at})()]

    def is_started(self):
        return True

    def waiting(self):
        return False

    def status_msg(self):
        return self.title

    def detail(self):
        return ""

    def playback(self):
        return {}

    def is_live(self):
        return False


class WatchedPlayer(player_module._Player):
    """The real player with the thread-starting part replaced."""

    def __init__(self):
        super().__init__()
        self.played = []
        self.built = []

    def _play(self, play, requested_at=None):
        self.played.append((play, requested_at))
        self.play_pipe = play.pipe
        self.playlist = play.playlist
        self.index = play.index
        self.error = None

    def buildYtdl(self, url, title=None, subs=None, sources=None):
        pipe = SourcePipe(list(sources or [url]), list(sources or [url]).index(url))
        pipe.subs = subs
        self.built.append((url, title, subs, sources))
        return pipe


@pytest.fixture
def player(monkeypatch):
    pl = WatchedPlayer()
    pl.play_requested_at = 100.0
    pl.playlist = ["the list"]
    pl.index = 2
    return pl


@pytest.fixture
def failures(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        playquality, "record_failed", lambda url, reason=None: recorded.append((url, reason))
    )
    return recorded


class TestFailover:
    def test_it_tries_the_next_mirror_before_the_picture(self, player, failures):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player.error = "Could not open the stream"
        assert player._fail_over("A Channel") is True
        assert failures == [("http://a", "Could not open the stream")]
        assert player.built == [("http://b", "A Channel", None, ["http://a", "http://b"])]
        assert player.played[0][0].pipe.sources == ["http://a", "http://b"]
        assert player.played[0][0].pipe.source_index == 1
        assert player._failover_note == "source 2 of 2"

    def test_the_count_keeps_the_original_request(self, player, failures):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player.error = "gone"
        player._fail_over("A Channel")
        assert player.played[0][1] == 100.0

    def test_the_playlist_place_is_kept(self, player, failures):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player.error = "gone"
        player._fail_over("A Channel")
        play = player.played[0][0]
        assert play.playlist == ["the list"]
        assert play.index == 2

    def test_the_subtitles_travel_to_the_next_attempt(self, player, failures):
        player.play_pipe = SourcePipe(
            ["http://a", "http://b"], index=0, subs={"lang": "eng"}
        )
        player.error = "gone"
        player._fail_over("A Channel")
        assert player.built[0][2] == {"lang": "eng"}

    def test_a_picture_that_started_is_not_restarted(self, player, failures):
        player.play_pipe = SourcePipe(
            ["http://a", "http://b"], index=0, media_at=120.0
        )
        player.error = "died later"
        assert player._fail_over("A Channel") is False
        assert failures == []
        assert player.played == []

    def test_a_row_without_mirrors_has_nowhere_to_go(self, player, failures):
        player.play_pipe = SourcePipe(["http://a"], index=0)
        player.error = "gone"
        assert player._fail_over("A Channel") is False
        assert failures == []

    def test_an_untracked_play_is_not_failed_over(self, player, failures):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0, track=False)
        player.error = "gone"
        assert player._fail_over("A Channel") is False

    def test_it_stops_after_the_cap(self, player, failures):
        sources = ["http://%d" % i for i in range(10)]
        player.play_pipe = SourcePipe(sources, index=player_module._FAILOVER_TRIES - 1)
        player.error = "gone"
        assert player._fail_over("A Channel") is False

    def test_no_pipe_is_not_a_failure(self, player, failures):
        player.play_pipe = None
        assert player._fail_over("A Channel") is False


class FakeThread:
    def join(self, timeout=None):
        pass

    def is_alive(self):
        return False


class TestTheStoppedPipe:
    """
    The one place that decides whether a stopped pipe is a failure.

    A stop reports any stage error, so a person pressing stop or skip while a
    stage was winding down looks exactly like a broken source from here. The
    difference is whether the stop was asked for, and it is the difference
    between "failed · 10m ago" and a lie in the list.
    """

    def _stopped(self, player, monkeypatch, error, ended=True, nextplay=None):
        calls = {"after": [], "failover": [], "played": [], "recorded": []}
        player.msgq.put(error)
        player.msgq.put(ended)
        player.play_thread = FakeThread()
        monkeypatch.setattr(
            player, "_after", lambda title, was_ended: calls["after"].append((title, was_ended))
        )
        monkeypatch.setattr(
            player, "_fail_over", lambda title: calls["failover"].append(title) or False
        )
        player._play = lambda play, requested_at=None: calls["played"].append(play)
        monkeypatch.setattr(
            playquality,
            "record_failed",
            lambda url, reason=None: calls["recorded"].append((url, reason)),
        )
        player._pipe_stopped(nextplay)
        return calls

    def test_an_error_nobody_asked_for_is_a_failed_source(
        self, player, monkeypatch
    ):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        calls = self._stopped(player, monkeypatch, "Could not open the stream")
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == [("http://a", "Could not open the stream")]

    def test_a_failover_tries_the_next_source_and_does_not_finish(
        self, player, monkeypatch
    ):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)

        def fail_over(title):
            return True

        player.msgq.put("gone")
        player.msgq.put(True)
        player.play_thread = FakeThread()
        after = []
        monkeypatch.setattr(player, "_after", lambda *args: after.append(args))
        monkeypatch.setattr(player, "_fail_over", fail_over)
        player._pipe_stopped(None)
        assert after == []

    def test_a_row_without_mirrors_is_not_written_down(self, player, monkeypatch):
        """The journal is about channels; a YouTube film has no mark to earn."""
        player.play_pipe = SourcePipe(["http://a"], index=0, track=False)
        calls = self._stopped(player, monkeypatch, "Could not open the stream")
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == []

    def test_a_stop_the_person_asked_for_is_not_a_failure(
        self, player, monkeypatch
    ):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player._stopped = True
        calls = self._stopped(player, monkeypatch, "Could not open the stream")
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == []
        assert calls["failover"] == []

    def test_a_replacement_play_is_not_a_failure(self, player, monkeypatch):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        calls = self._stopped(
            player, monkeypatch, "Could not open the stream", nextplay="new play"
        )
        assert calls["played"] == ["new play"]
        assert calls["after"] == []
        assert calls["recorded"] == []

    def test_an_end_with_no_error_is_not_a_failure(self, player, monkeypatch):
        player.play_pipe = SourcePipe(["http://a"], index=0)
        calls = self._stopped(player, monkeypatch, None)
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == []


class TestTimeToPicture:
    def test_the_success_is_recorded_against_this_attempt(self, player, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            playquality, "record_ok", lambda url, seconds: recorded.append((url, seconds))
        )
        player.play_pipe = SourcePipe(["http://a"], index=0, media_at=106.2)
        player.attempt_started_at = 100.0
        player.playlist = []
        player._note_picture()
        assert recorded[0][0] == "http://a"
        assert recorded[0][1] == pytest.approx(6.2)

    def test_an_untracked_play_is_not_recorded(self, player, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            playquality, "record_ok", lambda url, seconds: recorded.append((url, seconds))
        )
        player.play_pipe = SourcePipe(
            ["http://a"], index=0, media_at=106.2, track=False
        )
        player.attempt_started_at = 100.0
        player._note_picture()
        assert recorded == []

    def test_a_failover_note_goes_away_when_the_picture_starts(self, player, monkeypatch):
        monkeypatch.setattr(playquality, "record_ok", lambda url, seconds: None)
        player.play_pipe = SourcePipe(["http://a"], index=0, media_at=106.2)
        player.attempt_started_at = 100.0
        player._failover_note = "source 2 of 4"
        player._note_picture()
        assert player._failover_note is None

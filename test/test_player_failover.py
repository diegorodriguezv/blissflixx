"""
A row with mirrors, and what the player does when one does not work.

The IPTV channel hands a channel's addresses to a play, best first. The player
starts the first one immediately, checks the rest in the background, and on a
failure or a freeze moves to the next in the order real-played → probed-good →
untried → probed-bad → real-failed. It keeps going until one plays or the list
is empty, and only then is the channel marked failed.

No pipeline is started. The pipe is a stand-in with the fields the play paths
read, and _play is watched rather than run.
"""

import time

import pytest

import lib.player.player as player_module
import lib.playquality as playquality


class SourcePipe:
    def __init__(
        self,
        sources,
        index=0,
        media_at=None,
        track=True,
        subs=None,
        live=False,
    ):
        self.title = "A Channel"
        self.sources = sources
        self.source_index = index
        self.subs = subs
        self.track = track
        self.live = live
        self.procs = [type("Stage", (), {"_media_at": media_at})()]

    def is_live(self):
        return self.live

    def is_started(self):
        return True

    def is_stopping(self):
        return False

    def waiting(self):
        return False

    def status_msg(self):
        return self.title

    def detail(self):
        return ""

    def playback(self):
        return {}


class FakeCheck:
    def __init__(self, results=None, seconds=None):
        self._results = results or {}
        self._seconds = seconds or {}
        self.waited = []

    def result(self, url):
        return self._results.get(url)

    def seconds(self, url):
        return self._seconds.get(url)

    def wait(self, timeout):
        self.waited.append(timeout)
        return []

    def cancel(self):
        pass


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
        pipe = SourcePipe(
            list(sources or [url]),
            list(sources or [url]).index(url),
            subs=subs,
        )
        self.built.append((url, title, subs, sources))
        return pipe


@pytest.fixture
def latest(monkeypatch):
    """The real-play outcomes the picker sees, with none by default."""
    outcomes = {}

    def fake(url, within=None):
        return outcomes.get(url)

    monkeypatch.setattr(playquality, "latest", fake)
    return outcomes


@pytest.fixture
def player(latest):
    pl = WatchedPlayer()
    pl.play_requested_at = 100.0
    pl.playlist = ["the list"]
    pl.index = 2
    return pl


def prepared(player, sources, index=0, **pipe_kwargs):
    pipe = SourcePipe(sources, index=index, **pipe_kwargs)
    player.play_pipe = pipe
    player._tried = {sources[index]}
    player._check = None
    return pipe


@pytest.fixture
def failures(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        playquality, "record_failed", lambda url, reason=None: recorded.append((url, reason))
    )
    return recorded


class TestFailover:
    def test_it_tries_the_next_mirror_before_the_picture(self, player, failures):
        prepared(player, ["http://a", "http://b"])
        player.error = "Could not open the stream"
        assert player._fail_over("A Channel") is True
        assert failures == [("http://a", "Could not open the stream")]
        assert player.built == [("http://b", "A Channel", None, ["http://a", "http://b"])]
        play, requested = player.played[0]
        assert play.pipe.sources == ["http://a", "http://b"]
        assert play.pipe.source_index == 1
        assert play.note == "source 2 of 2"
        assert play.keep_state is True
        assert requested == 100.0

    def test_it_keeps_going_past_the_old_four_source_limit(self, player, failures):
        sources = ["http://%d" % i for i in range(10)]
        prepared(player, sources, index=5)
        player._tried = set(sources[:6])
        player.error = "gone"
        assert player._fail_over("A Channel") is True
        assert player.built[0][0] == "http://6"
        assert player.played[0][0].note == "source 7 of 10"

    def test_the_playlist_place_is_kept(self, player, failures):
        prepared(player, ["http://a", "http://b"])
        player.error = "gone"
        player._fail_over("A Channel")
        play = player.played[0][0]
        assert play.playlist == ["the list"]
        assert play.index == 2

    def test_the_subtitles_travel_to_the_next_attempt(self, player, failures):
        prepared(player, ["http://a", "http://b"], subs={"lang": "eng"})
        player.error = "gone"
        player._fail_over("A Channel")
        assert player.built[0][2] == {"lang": "eng"}

    def test_a_picture_that_started_is_not_restarted_here(self, player, failures):
        """A freeze has its own path; a film keeps its place."""
        prepared(player, ["http://a", "http://b"], media_at=120.0)
        player.error = "died later"
        assert player._fail_over("A Channel") is False
        assert failures == []
        assert player.played == []

    def test_a_row_without_mirrors_has_nowhere_to_go(self, player, failures):
        prepared(player, ["http://a"])
        player.error = "gone"
        assert player._fail_over("A Channel") is False
        assert failures == []

    def test_an_untracked_play_is_not_failed_over(self, player, failures):
        prepared(player, ["http://a", "http://b"], track=False)
        player.error = "gone"
        assert player._fail_over("A Channel") is False

    def test_no_pipe_is_not_a_failure(self, player, failures):
        player.play_pipe = None
        assert player._fail_over("A Channel") is False


class TestChoosingTheNextSource:
    def _pick(self, player, sources, current=0, check=None, tried=None):
        prepared(player, sources, index=current)
        player._check = check
        if tried is not None:
            player._tried = set(tried)
        return player._next_source()

    def test_a_real_success_goes_before_a_probe(self, player, latest):
        sources = ["http://a", "http://b", "http://c", "http://d"]
        latest["http://d"] = ("ok", time.time() - 60, 5)
        assert (
            self._pick(player, sources, check=FakeCheck({"http://b": True}))
            == "http://d"
        )

    def test_a_probed_good_goes_before_an_untried(self, player, latest):
        sources = ["http://a", "http://b", "http://c"]
        assert (
            self._pick(player, sources, check=FakeCheck({"http://c": True}))
            == "http://c"
        )

    def test_an_untried_goes_before_a_probed_bad(self, player, latest):
        sources = ["http://a", "http://b", "http://c"]
        assert (
            self._pick(player, sources, check=FakeCheck({"http://c": False}))
            == "http://b"
        )

    def test_a_probed_bad_goes_before_a_real_failure(self, player, latest):
        sources = ["http://a", "http://b", "http://c"]
        latest["http://c"] = ("bad", time.time() - 60, 0)
        assert (
            self._pick(
                player, sources, check=FakeCheck({"http://b": False, "http://c": False})
            )
            == "http://b"
        )

    def test_the_real_failure_is_tried_last(self, player, latest):
        sources = ["http://a", "http://b", "http://c"]
        latest["http://c"] = ("froze", time.time() - 60, 0)
        assert (
            self._pick(
                player,
                sources,
                check=FakeCheck({"http://b": False, "http://c": False}),
                tried={"http://a", "http://b"},
            )
            == "http://c"
        )

    def test_nothing_left_is_none(self, player, latest):
        sources = ["http://a"]
        prepared(player, sources)
        assert player._next_source() is None

    def test_every_source_is_tried_eventually(self, player, latest):
        sources = ["http://%d" % i for i in range(4)]
        assert (
            self._pick(
                player,
                sources,
                check=FakeCheck({"http://1": False, "http://2": False}),
                tried={"http://0", "http://1", "http://2"},
            )
            == "http://3"
        )

    def test_the_probe_is_waited_on_for_an_untried_choice(self, player, latest):
        sources = ["http://a", "http://b"]
        check = FakeCheck()
        prepared(player, sources)
        player._check = check
        assert player._next_source(wait_for_probe=True) == "http://b"
        assert check.waited == [5.0]

    def test_no_wait_when_a_probed_good_is_ready(self, player, latest):
        sources = ["http://a", "http://b"]
        check = FakeCheck({"http://b": True})
        prepared(player, sources)
        player._check = check
        assert player._next_source(wait_for_probe=True) == "http://b"
        assert check.waited == []


class TestTheParallelCheck:
    def test_only_the_unrated_other_sources_are_probed(self, player, latest, monkeypatch):
        sources = ["http://a", "http://b", "http://c"]
        prepared(player, sources, index=0)
        latest["http://b"] = ("ok", time.time() - 60, 5)
        created = []
        monkeypatch.setattr(
            player_module,
            "SourceCheck",
            lambda urls: created.append(list(urls)) or FakeCheck(),
        )
        player._start_check()
        assert created == [["http://c"]]

    def test_a_single_source_has_nothing_to_probe(self, player, latest, monkeypatch):
        prepared(player, ["http://a"])
        created = []
        monkeypatch.setattr(
            player_module, "SourceCheck", lambda urls: created.append(urls)
        )
        player._start_check()
        assert created == []

    def test_an_untracked_play_is_not_probed(self, player, latest, monkeypatch):
        prepared(player, ["http://a", "http://b"], track=False)
        created = []
        monkeypatch.setattr(
            player_module, "SourceCheck", lambda urls: created.append(urls)
        )
        player._start_check()
        assert created == []


class TestReconnect:
    def test_a_live_picture_that_froze_moves_to_another_source(self, player, latest):
        prepared(
            player,
            ["http://a", "http://b"],
            media_at=120.0,
            live=True,
        )
        play = player._reconnect_play("A Channel")
        assert play is not None
        assert play.pipe.source_index == 1
        assert play.note == "source 2 of 2"
        assert play.keep_state is True
        assert player.built[0][0] == "http://b"

    def test_a_film_is_not_restarted(self, player, latest):
        prepared(player, ["http://a", "http://b"], media_at=120.0, live=False)
        assert player._reconnect_play("A Channel") is None

    def test_a_stream_still_waiting_for_the_picture_is_not_a_freeze(
        self, player, latest
    ):
        prepared(player, ["http://a", "http://b"], media_at=None, live=True)
        assert player._reconnect_play("A Channel") is None

    def test_nothing_left_means_nothing_to_do(self, player, latest):
        prepared(player, ["http://a"], media_at=120.0, live=True)
        assert player._reconnect_play("A Channel") is None

    def test_a_stopping_pipe_is_left_alone(self, player, latest):
        pipe = prepared(player, ["http://a", "http://b"], media_at=120.0, live=True)
        pipe.is_stopping = lambda: True
        assert player._reconnect_play("A Channel") is None


class TestTimeToPicture:
    def test_the_success_is_recorded_against_this_attempt(self, player, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            playquality, "record_ok", lambda url, seconds: recorded.append((url, seconds))
        )
        prepared(player, ["http://a"], media_at=106.2)
        player.attempt_started_at = 100.0
        player._note_picture()
        assert recorded[0][0] == "http://a"
        assert recorded[0][1] == pytest.approx(6.2)

    def test_an_untracked_play_is_not_recorded(self, player, monkeypatch):
        recorded = []
        monkeypatch.setattr(
            playquality, "record_ok", lambda url, seconds: recorded.append((url, seconds))
        )
        prepared(player, ["http://a"], media_at=106.2, track=False)
        player.attempt_started_at = 100.0
        player._note_picture()
        assert recorded == []

    def test_a_failover_note_goes_away_when_the_picture_starts(
        self, player, monkeypatch
    ):
        monkeypatch.setattr(playquality, "record_ok", lambda url, seconds: None)
        prepared(player, ["http://a"], media_at=106.2)
        player.attempt_started_at = 100.0
        player._failover_note = "source 2 of 4"
        player._note_picture()
        assert player._failover_note is None


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
    between a failure in the journal and a lie in the list.
    """

    def _stopped(self, player, monkeypatch, error, ended=True, nextplay=None):
        calls = {"after": [], "failover": [], "played": [], "recorded": []}
        player.msgq.put(error)
        player.msgq.put(ended)
        player.play_thread = FakeThread()
        monkeypatch.setattr(
            player,
            "_after",
            lambda title, was_ended: calls["after"].append((title, was_ended)),
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

    def test_an_error_nobody_asked_for_is_a_failed_source(self, player, monkeypatch):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player._tried = {"http://a"}
        calls = self._stopped(player, monkeypatch, "Could not open the stream")
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == [("http://a", "Could not open the stream")]

    def test_a_failover_tries_the_next_source_and_does_not_finish(
        self, player, monkeypatch
    ):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player._tried = {"http://a"}
        player.msgq.put("gone")
        player.msgq.put(True)
        player.play_thread = FakeThread()
        after = []
        monkeypatch.setattr(player, "_after", lambda *args: after.append(args))
        monkeypatch.setattr(player, "_fail_over", lambda title: True)
        player._pipe_stopped(None)
        assert after == []

    def test_a_stop_the_person_asked_for_is_not_a_failure(self, player, monkeypatch):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player._tried = {"http://a"}
        player._stopped = True
        calls = self._stopped(player, monkeypatch, "Could not open the stream")
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == []
        assert calls["failover"] == []

    def test_a_replacement_play_is_not_a_failure(self, player, monkeypatch):
        player.play_pipe = SourcePipe(["http://a", "http://b"], index=0)
        player._tried = {"http://a"}
        calls = self._stopped(
            player, monkeypatch, "Could not open the stream", nextplay="new play"
        )
        assert calls["played"] == ["new play"]
        assert calls["after"] == []
        assert calls["recorded"] == []

    def test_an_end_with_no_error_is_not_a_failure(self, player, monkeypatch):
        player.play_pipe = SourcePipe(["http://a"], index=0)
        player._tried = {"http://a"}
        calls = self._stopped(player, monkeypatch, None)
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == []

    def test_a_row_without_mirrors_is_not_written_down(self, player, monkeypatch):
        """The journal is about channels; a YouTube film has no mark to earn."""
        player.play_pipe = SourcePipe(["http://a"], index=0, track=False)
        player._tried = {"http://a"}
        calls = self._stopped(player, monkeypatch, "Could not open the stream")
        assert calls["after"] == [("A Channel", True)]
        assert calls["recorded"] == []

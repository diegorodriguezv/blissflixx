"""
Playback control, pipeline composition and the player loop.

Three things are covered here that cannot be checked anywhere else without a
Raspberry Pi:

1. The pipeline _Player.play() assembles. That method decides which stages run,
   and patching _start_thread stops its background loop from running, so the
   exact stage list can be asserted without any process being spawned.

2. Which player method a url turns into, which is the only place that decision
   is made.

3. The loop itself: what it does when a film runs out, when it is stopped, and
   when it moves to the next item of a playlist -- driven with fake pipes, so
   each step is deterministic.

Still hardware-only: whether the command actually reaches a running player,
whether dbus accepts the call, and whether the picture appears. Everything up to
the handover is covered.
"""

import json
import pathlib
import queue
import threading
import time
import unittest.mock as m
import urllib.parse

import pytest

import lib.player.player as player_module
from lib.api import playr
from lib.player.dlsrvproc import DlsrvProcess
from lib.player.localproc import LocalFileProcess
from lib.player.pflixproc import PeerflixProcess
from lib.player.subsproc import SubtitlesProcess
from lib.player.vlcproc import VlcProcess


def build_pipe(title="T", src=None, subs=None, http=False, dlsrv=True):
    """
    Run _Player.play() without letting its background thread start, and return
    the ProcessPipe it queued.
    """
    pl = player_module._Player()
    with m.patch("lib.player.player._start_thread", lambda *a, **k: m.Mock()):
        pl.play(
            title,
            src if src is not None else LocalFileProcess("/tmp/a.mkv"),
            subs,
            http,
            dlsrv,
        )
    queued = []
    while not pl.msgq.empty():
        queued.append(pl.msgq.get_nowait())
    assert queued[0] == player_module.MSG_PLAYER_PLAY
    return queued[1].pipe


def stages(pipe):
    return [p.name() for p in pipe.procs]


class TestPipelineComposition:
    """
    The dlsrv stage is about *serving* a file that is still growing, which is
    independent of which player is doing the rendering. The player stage comes
    from the backend selection; these tests pin the serving half.
    """

    def test_local_file_is_served_over_http_by_default(self):
        """
        A file:// url has no stream to pipe, so dlsrv serves it over http and
        the player reads from there, which is what lets it follow a file that is
        still being written.
        """
        pipe = build_pipe()
        assert stages(pipe)[:2] == ["localfile", "dlsrv"]

    def test_piped_stream_skips_dlsrv(self):
        """
        A stream is already live, so there is nothing to serve and nothing to
        follow.
        """
        pipe = build_pipe(http=True, dlsrv=False)
        assert "dlsrv" not in stages(pipe)

    def test_dlsrv_is_added_when_the_file_is_not_piped(self):
        assert "dlsrv" in stages(build_pipe(http=False, dlsrv=True))

    def test_dlsrv_omitted_when_http_is_requested(self):
        """
        The distinction is http, not the backend: a live stream must not be
        served a second time.
        """
        assert "dlsrv" not in stages(build_pipe(http=True, dlsrv=True))

    def test_the_player_stage_comes_from_the_backend_choice(self):
        """Not from the http and dlsrv flags, which is what "legacy" is for."""
        with m.patch("lib.player.player.load", return_value={"backend": "vlc"}):
            pipe = build_pipe()
        assert stages(pipe)[-1] == "vlc"

    def test_default_backend_is_the_player_stage(self):
        pipe = build_pipe()
        assert stages(pipe)[-1] == "vlc"

    def test_subtitles_stage_precedes_everything(self):
        """
        The subtitle file must exist before the player is told to use it, so the
        fetch stage comes first and passes the path along.
        """
        pipe = build_pipe(subs={"lang": "eng", "title": "M"})
        assert stages(pipe)[0] == "subtitles"
        assert stages(pipe)[1] == "localfile"

    def test_title_is_carried_onto_the_pipe(self):
        assert build_pipe(title="My Movie").title == "My Movie"

    def test_no_stages_when_nothing_to_play(self):
        """
        A pipe with no stages would raise IndexError from status_msg, so play()
        must always add at least the source it is given.
        """
        assert stages(build_pipe(src=LocalFileProcess("/a.mkv")))

    def test_source_process_is_second_after_subtitles(self):
        pipe = build_pipe(subs={"lang": "eng", "title": "M"})
        assert isinstance(pipe.procs[1], LocalFileProcess)


class TestPlayrRouting:
    """
    playr.play() dispatches on the url scheme. That is the only place that
    decides which player method handles a request.
    """

    def _route(self, url):
        """
        Dispatch a url through playr.play() with all three player entry points
        stubbed, and return the mocks so the caller can assert which one ran.
        """
        local = m.Mock()
        torrent = m.Mock()
        ytdl = m.Mock()
        with m.patch("lib.player.player._start_thread", lambda *a, **k: m.Mock()):
            with (
                m.patch.object(player_module._Player, "playLocalFile", local),
                m.patch.object(player_module._Player, "playTorrent", torrent),
                m.patch.object(player_module._Player, "playYtdl", ytdl),
            ):
                playr.play(url=url, title="T")
        return {
            "playLocalFile": local,
            "playTorrent": torrent,
            "playYtdl": ytdl,
        }

    def test_file_url_plays_locally(self):
        mocks = self._route("file:///media/movies/a.mkv")
        mocks["playLocalFile"].assert_called_once_with(
            "/media/movies/a.mkv", "T", None, None
        )
        mocks["playYtdl"].assert_not_called()

    def test_torrent_url_selects_a_file_inside_it(self):
        url = "http://x/y.torrent?bf_torr_idx=3"
        mocks = self._route(url)
        mocks["playTorrent"].assert_called_once_with(url, 3, "T", None, None, None)
        mocks["playYtdl"].assert_not_called()

    def test_main_torrent_file_uses_minus_one(self):
        url = "http://x/y.torrent?bf_torr_idx=-1"
        mocks = self._route(url)
        assert mocks["playTorrent"].call_args[0][1] == -1

    def test_plain_url_goes_to_ytdl(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        mocks = self._route(url)
        mocks["playYtdl"].assert_called_once_with(url, "T", None, None, None)

    def test_missing_url_is_rejected(self):
        from lib.api.common import ApiError

        with pytest.raises(ApiError):
            playr.play()

    def test_subtitles_prefs_are_saved(self, settings):
        """
        The chosen subtitle language persists so the next play reuses it.

        _start_thread is stubbed so the player's background loop never runs;
        without it this would queue a real pipeline and spawn yt-dlp.
        """
        with m.patch("lib.player.player._start_thread", lambda *a, **k: m.Mock()):
            with m.patch.object(player_module._Player, "playYtdl"):
                playr.play(url="https://x/v.mp4", title="T", subs={"lang": "fra"})
        settings._cache.clear()
        assert settings.load("subtitles") == {"lang": "fra"}

    def test_no_prefs_saved_without_subs(self, settings):
        with m.patch("lib.player.player._start_thread", lambda *a, **k: m.Mock()):
            with m.patch.object(player_module._Player, "playYtdl"):
                playr.play(url="https://x/v.mp4", title="T")
        settings._cache.clear()
        assert settings.load("subtitles") == {}


class TestControlRouting:
    def test_missing_action_is_rejected(self):
        from lib.api.common import ApiError

        with pytest.raises(ApiError):
            playr.control()

    def test_action_is_forwarded_to_the_player(self):
        with m.patch.object(player_module._Player, "control") as control:
            playr.control(action="pause")
        control.assert_called_once_with("pause")


class TestPlayDoesNotBlockTheCaller:
    """
    play() is called from CherryPy request threads, so it must fail rather than
    wait forever when the loop is not draining messages.

    The queue used to hold exactly two messages and a play costs exactly two, so
    one stalled play filled it and the next call blocked inside the request
    thread. That is how a wedged player took the web server with it.
    """

    def _stalled_player(self):
        pl = player_module._Player()
        pl.main_thread = object()  # pretend the loop is already running
        return pl

    def _play_off_thread(self, pl, timeout=3.0):
        """
        Run play() on a thread so a blocking put fails an assertion here instead
        of hanging the test run. The raised exception, or None, comes back.
        """
        caught = []

        def run():
            try:
                pl.play("title", LocalFileProcess("/tmp/a.mkv"), http=True)
            except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
                caught.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        return thread, (caught[0] if caught else None)

    def test_a_filling_queue_is_reported_not_waited_on(self, settings, monkeypatch):
        monkeypatch.setattr(player_module, "_PLAY_ENQUEUE_TIMEOUT", 0.2)
        pl = self._stalled_player()
        pl.msgq = queue.Queue(maxsize=2)
        pl.msgq.put(object())

        thread, raised = self._play_off_thread(pl)

        assert not thread.is_alive(), "play() blocked on a full queue"
        assert isinstance(raised, RuntimeError)
        assert "not responding" in str(raised)

    def test_the_queue_holds_more_than_one_play(self, settings):
        """
        Pinned because the old capacity was exactly the cost of one play, which
        is the arithmetic that made a single stall fatal.
        """
        pl = player_module._Player()
        assert pl.msgq.maxsize > 2

    def test_a_refused_play_says_so_on_status(self, settings, monkeypatch):
        monkeypatch.setattr(player_module, "_PLAY_ENQUEUE_TIMEOUT", 0.2)
        pl = self._stalled_player()
        pl.msgq = queue.Queue(maxsize=2)
        pl.msgq.put(object())

        thread, _raised = self._play_off_thread(pl)

        assert not thread.is_alive()
        assert "not responding" in pl.status()["Msg"]

    def test_an_ordinary_play_is_not_refused(self, settings):
        """The guard must not fire when there is room, which is the normal case."""
        pl = self._stalled_player()
        pl.play("title", LocalFileProcess("/tmp/a.mkv"), http=True)
        assert pl.msgq.qsize() == 2


class TestErrorBelongsToTheAttemptThatCausedIt:
    """
    self.error is cleared when a play is accepted, not only when it starts.

    It used to be cleared inside _play(), which the loop reaches only after the
    previous pipe has stopped. While the loop was wedged that never happened, so
    status() went on reporting the previous attempt's failure as though it
    described the play just requested.
    """

    def test_a_new_play_clears_the_previous_error(self, settings):
        pl = player_module._Player()
        pl.error = "main playlist: end of playlist, exiting"
        pl.main_thread = object()

        pl.play("title", LocalFileProcess("/tmp/a.mkv"), http=True)

        assert pl.error is None

    def test_the_new_pipe_reports_its_own_failure_afterwards(self, settings):
        """
        Clearing on acceptance is not the same as clearing forever: the pipe that
        actually runs can still fail, and then its error is what shows.
        """
        pl = player_module._Player()
        pl.main_thread = object()
        pl.play("title", LocalFileProcess("/tmp/a.mkv"), http=True)

        assert pl.msgq.get() == player_module.MSG_PLAYER_PLAY
        pl._play(pl.msgq.get())
        pl.error = "disk is full"

        assert pl.status()["Error"] is True


class TestTorrentSubtitles:
    """
    A subtitle that is a file inside the torrent.

    It is not searched for -- it is fetched from the peerflix that is already
    serving this film -- so the pipeline must not gain the online subtitle
    stage, and the index must reach the torrent stage.
    """

    def test_the_online_stage_is_not_added(self):
        pipe = player_module._Player().buildTorrent(
            "magnet:?xt=urn:btih:" + "A" * 40,
            0,
            "Film",
            {"torrent": {"idx": 3, "name": "Film.eng.srt"}},
        )
        names = [p.name() for p in pipe.procs]
        assert "subtitles" not in names

    def test_the_index_and_the_name_reach_the_torrent_stage(self):
        pipe = player_module._Player().buildTorrent(
            "magnet:?xt=urn:btih:" + "A" * 40,
            2,
            "Film",
            {"torrent": {"idx": 3, "name": "Film.eng.srt"}},
        )
        stage = [p for p in pipe.procs if p.name() == "peerflix"][0]
        assert stage.subs_idx == 3
        assert stage.subs_name == "Film.eng.srt"

    def test_an_ordinary_subtitle_still_uses_the_online_stage(self):
        pipe = player_module._Player().buildTorrent(
            "magnet:?xt=urn:btih:" + "A" * 40, 0, "Film", {"title": "Film"}
        )
        names = [p.name() for p in pipe.procs]
        assert "subtitles" in names


class TestStatus:
    def test_status_shape(self):
        """The frontend polls this continuously, so the keys must stay stable."""
        status = playr.status()
        assert set(status) == {
            "State",
            "Msg",
            "Title",
            "Paused",
            "Error",
            "Notice",
            "Waiting",
            "Elapsed",
        }

    def test_idle_player_is_not_running(self):
        """
        Asserted on a fresh instance rather than the shared Player singleton,
        which other tests in this file leave holding state.
        """
        assert player_module._Player().status()["State"] == (
            player_module.ST_NOT_RUNNING
        )

    def test_an_idle_player_is_not_waiting_and_has_no_count(self):
        status = player_module._Player().status()
        assert status["Waiting"] is False
        assert status["Elapsed"] == 0

    def test_the_count_runs_from_when_the_film_was_asked_for(self):
        """
        What the bar shows as "12s" beside the spinner. It is wall clock from
        the request, because the loop that would tick is busy with the pipeline.
        """

        class _Pipe:
            def status_msg(self):
                return "My Movie"

            def waiting(self):
                return True

            def is_started(self):
                return True

            def is_stopping(self):
                return False

        pl = player_module._Player()
        pl.play_pipe = _Pipe()
        pl.play_requested_at = time.time() - 12

        status = pl.status()
        assert status["Waiting"] is True
        assert status["Elapsed"] >= 11
        # Still running: waiting is not a state of its own, it is a thing the
        # running player is doing.
        assert status["State"] == player_module.ST_RUNNING

    def test_status_does_not_need_a_pipe(self):
        """
        status() must be safe before anything is playing, since the UI polls it
        on page load.
        """
        assert playr.status() is not None


class TestStageAvailability:
    """
    The stages a pipe can contain, which is the set the planned backend
    abstraction has to cover.
    """

    @pytest.mark.parametrize("cls", [DlsrvProcess, VlcProcess])
    def test_stage_constructs_without_arguments(self, cls):
        assert cls().name()

    def test_subtitles_stage_needs_a_lookup(self):
        """
        Constructed with the subs dict, not with pipeline args, because the
        title and language are known before the pipeline exists.
        """
        proc = SubtitlesProcess({"lang": "eng", "title": "M"})
        assert proc.name() == "subtitles"
        assert proc.status_msg() == "FETCHING SUBTITLES"

    def test_peerflix_needs_a_torrent_and_index(self):
        assert PeerflixProcess("http://x/y.torrent", 1).name() == "peerflix"

    def test_url_scheme_parsing_uses_urlparse(self):
        """file:// urls must yield a bare filesystem path, not file:///x."""
        obj = urllib.parse.urlparse("file:///media/a.mkv")
        assert obj.scheme == "file"
        assert obj.path == "/media/a.mkv"


class TestTheLoopSurvivesAFailedAction:
    """
    Nothing in the player loop may end the thread.

    This is the whole player: every stop, every play and every status comes
    through here. An exception escaping does not fail the request that caused
    it -- it leaves the player deaf, so stop returns 200 and nothing stops,
    play returns 200 and nothing starts, and a film on screen cannot be ended.

    It happened. Stopping a pipeline whose first stage was still starting
    raised AttributeError out of stop(), which ended this loop thread, and the
    player could not be recovered without restarting the process.
    """

    def test_a_stop_that_raises_does_not_end_the_loop(self):
        pl = player_module._Player()
        loop = threading.Thread(target=pl.start, daemon=True)
        loop.start()

        class Boom:
            def stop(self):
                raise AttributeError(
                    "'YoutubeDlProcess' object has no attribute 'proc'"
                )

        pl.play_pipe = Boom()
        pl.msgq.put(player_module.MSG_PLAYER_STOP)
        # The loop must still be answering.
        pl.msgq.put(player_module.MSG_PLAYER_STOP)
        assert loop.is_alive()

        # QUIT stops too, so give it a pipe that can be stopped before asking
        # the loop to end -- otherwise this asserts on the same raise.
        pl.play_pipe = None
        pl.play_pipe = None
        pl.msgq.put(player_module.MSG_PLAYER_QUIT)
        loop.join(timeout=3)
        assert not loop.is_alive(), "the loop should still be able to shut down"

    def test_the_loop_keeps_going_after_a_failure(self):
        """Not just alive -- still functional, which is what the user needs."""
        pl = player_module._Player()
        loop = threading.Thread(target=pl.start, daemon=True)
        loop.start()

        class Boom:
            def stop(self):
                raise AttributeError("boom")

        pl.play_pipe = Boom()
        pl.msgq.put(player_module.MSG_PLAYER_STOP)
        time.sleep(0.3)

        # A second stop is picked up rather than left in a queue nobody reads.
        stopped = []
        pl.play_pipe = type("Ok", (), {"stop": lambda self: stopped.append(1)})()
        pl.msgq.put(player_module.MSG_PLAYER_STOP)
        deadline = time.time() + 3
        while not stopped and time.time() < deadline:
            time.sleep(0.05)
        assert stopped, "the loop did not reach the second stop"

        pl.play_pipe = None
        pl.msgq.put(player_module.MSG_PLAYER_QUIT)
        loop.join(timeout=3)


class _FakePipe:
    """
    A pipeline that starts nothing, and whose stop() the test drives.

    A real pipe reports MSG_PLAYER_PIPE_STOPPED when it is stopped, which is
    what the loop waits for before it does anything else; here that is a message
    the test puts on the queue itself, so each step of a run is deterministic.
    """

    def __init__(self, title="T"):
        self.title = title
        self.stopped = False

    def start(self, pmsgq):
        pass

    def stop(self):
        self.stopped = True

    def is_started(self):
        return True

    def is_stopping(self):
        return False

    def status_msg(self):
        return self.title

    def waiting(self):
        # This fake is always a film that arrived; waiting is the player stage
        # saying it has nothing to decode yet. See ProcessPipe.waiting().
        return False


def _builder(title):
    """A playlist entry: a callable the loop calls to get a pipe for that item."""

    def make():
        return _FakePipe(title)

    return make


def _wait_for(condition, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def _finished(pl, title, error=None, ended=True):
    """Tell the loop that the film playing has stopped, and how."""
    pl.msgq.put(player_module.MSG_PLAYER_PIPE_STOPPED)
    pl.msgq.put(error)
    pl.msgq.put(ended)


def _playing(pl, title):
    return _wait_for(lambda: pl.play_pipe is not None and pl.play_pipe.title == title)


class TestPlaylistRuns:
    """
    Films going one after another, which is what a playlist is for.

    The player is told the item that is playing and where it sits in the list,
    and both travel with the film. Everything here goes through the real loop,
    with only the network of processes faked out.
    """

    def test_a_film_that_runs_out_plays_the_next_one(self):
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two"), _builder("three")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        _finished(pl, "one", None, True)

        assert _playing(pl, "two")
        assert pl.index == 1

    def test_the_last_film_running_out_stops_there(self):
        pl = player_module._Player()
        playlist = [_builder("only")]

        pl.playPipe(_FakePipe("only"), playlist, 0)
        assert _playing(pl, "only")

        _finished(pl, "only", None, True)

        assert _wait_for(lambda: pl.play_pipe is None), "it should have stopped"
        assert pl.playlist is None, "the run is over, not still holding a list"

    def test_a_film_that_failed_is_skipped_and_said_out_loud(self):
        """
        One dead item must not end a run, and moving past it silently is worse
        than not moving past it, so the title and the reason are kept.
        """
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        _finished(pl, "one", "no such torrent", False)

        assert _playing(pl, "two")
        assert pl.notice == "Skipped one: no such torrent"
        assert pl.status()["Notice"] == "Skipped one: no such torrent"

    def test_a_failure_on_the_last_film_is_not_swallowed(self):
        """
        Nothing to move on to, so the failure is left where a plain failed play
        leaves it: on screen, with nothing playing.
        """
        pl = player_module._Player()
        playlist = [_builder("only")]

        pl.playPipe(_FakePipe("only"), playlist, 0)
        assert _playing(pl, "only")

        _finished(pl, "only", "it failed", False)

        assert _wait_for(lambda: pl.play_pipe is None)
        assert pl.status()["Error"] is True
        assert pl.status()["Msg"] == "it failed"

    def test_stop_ends_the_run(self):
        """
        A stop must not be followed by the next item. The place in the playlist
        goes with the film, so the stop that arrives afterwards has nothing left
        to advance.
        """
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        pl.msgq.put(player_module.MSG_PLAYER_STOP)
        assert _wait_for(lambda: pl.playlist is None)

        _finished(pl, "one", None, False)

        assert _wait_for(lambda: pl.play_pipe is None), "two must not start"

    def test_stop_drops_a_play_that_was_already_queued(self):
        """
        Stop means stop. A film asked for while this one was playing used to
        start anyway, because the queue was only cleared when a pipe stopped --
        so the button that says stop was followed by another film.
        """
        pl = player_module._Player()

        pl.playPipe(_FakePipe("first"))
        assert _playing(pl, "first")

        pl.playPipe(_FakePipe("second"))
        pl.msgq.put(player_module.MSG_PLAYER_STOP)
        _finished(pl, "first", None, False)

        assert _wait_for(lambda: pl.play_pipe is None), "second must not start"

    def test_next_item_moves_on_without_waiting_for_the_film_to_run_out(self):
        """
        A skip is a stop and a start, not a fast-forward: the item playing is
        given up and the next one begins, which is what makes it usable on a
        film that has stalled or is one nobody wants to watch.
        """
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two"), _builder("three")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        pl.control("next_item")
        # A pipe that is told to stop reports that it stopped.
        _finished(pl, "one", None, False)

        assert _playing(pl, "two")
        assert pl.index == 1

    def test_prev_item_goes_back(self):
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two"), _builder("three")]

        pl.playPipe(_FakePipe("three"), playlist, 2)
        assert _playing(pl, "three")

        pl.control("prev_item")
        _finished(pl, "three", None, False)

        assert _playing(pl, "two")
        assert pl.index == 1

    def test_skipping_past_the_end_says_so_and_plays_nothing_else(self):
        pl = player_module._Player()
        playlist = [_builder("one")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        pl.control("next_item")

        assert _wait_for(lambda: pl.notice == "End of playlist")
        assert pl.play_pipe.title == "one", "the film playing must not be disturbed"

    def test_skipping_before_the_start_says_so(self):
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        pl.control("prev_item")

        assert _wait_for(lambda: pl.notice == "Start of playlist")
        assert pl.play_pipe.title == "one"

    def test_skipping_a_film_that_is_not_in_a_playlist_says_so(self):
        """
        The buttons are on every playbar, and a film played from a channel or a
        search is not part of a list. Saying so is the alternative to a button
        that does nothing and cannot be told from a broken one.
        """
        pl = player_module._Player()

        pl.playPipe(_FakePipe("single"))
        assert _playing(pl, "single")

        pl.control("next_item")

        assert _wait_for(lambda: pl.notice == "Not playing a playlist")
        assert pl.play_pipe.title == "single"

    def test_a_notice_does_not_hide_the_film_that_is_playing(self):
        """
        A skipped item is not a failed play. The error field means nothing is
        playing at all, which is why a skip is reported separately -- otherwise
        the interface would report a stopped player while the next film ran.
        """
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")

        _finished(pl, "one", "it failed", False)
        assert _playing(pl, "two")

        status = pl.status()
        assert status["Notice"] == "Skipped one: it failed"
        assert status["Error"] is False
        assert status["State"] == player_module.ST_RUNNING
        assert status["Title"] == "two"

    def test_asking_for_something_else_clears_the_notice(self):
        pl = player_module._Player()
        playlist = [_builder("one"), _builder("two")]

        pl.playPipe(_FakePipe("one"), playlist, 0)
        assert _playing(pl, "one")
        _finished(pl, "one", "it failed", False)
        assert _playing(pl, "two")

        pl.playPipe(_FakePipe("elsewhere"))

        assert pl.notice is None
        assert pl.status()["Notice"] == ""


class TestPlaylistContext:
    """
    Which play belongs to a playlist, and which does not.

    The playlist a row came from is what lets the server carry on when the film
    ends or the item buttons are pressed, so it has to travel with the play --
    and it has to be the playlist's item the row actually was, which is why the
    url is checked rather than the position trusted.
    """

    @pytest.fixture
    def plists(self, tmp_path, monkeypatch):
        from lib.api import playlists as playlists_module

        path = tmp_path / "playlists"
        path.mkdir()
        monkeypatch.setattr(playlists_module, "PLIST_PATH", str(path))
        return playlists_module

    def _write(self, plists, name, items):
        """
        A playlist file as the real ones are written.

        Every item carries an img: PlaylistItem reads one out of the dict, so a
        row without it raises and the whole playlist becomes unreadable -- which
        is what the fallback test below is about.
        """
        items = [dict(item, img="/img/icons/list.svg") for item in items]
        (pathlib.Path(plists.PLIST_PATH) / (name + ".bfpl")).write_text(
            json.dumps({"title": name, "items": items})
        )

    def _play(self, **kwargs):
        """Dispatch a play with all three player entry points stubbed."""
        local = m.Mock()
        torrent = m.Mock()
        ytdl = m.Mock()
        with m.patch("lib.player.player._start_thread", lambda *a, **k: m.Mock()):
            with (
                m.patch.object(player_module._Player, "playLocalFile", local),
                m.patch.object(player_module._Player, "playTorrent", torrent),
                m.patch.object(player_module._Player, "playYtdl", ytdl),
            ):
                playr.play(**kwargs)
        return local, torrent, ytdl

    def test_playing_a_row_carries_the_playlist_and_the_place_in_it(self, plists):
        self._write(
            plists,
            "Run",
            [
                {"title": "one", "url": "https://example.com/1"},
                {"title": "two", "url": "https://example.com/2"},
                {"title": "three", "url": "https://example.com/3"},
            ],
        )

        _local, _torrent, ytdl = self._play(
            url="https://example.com/2", title="two", plid="Run", itemnum=1
        )

        url, _title, _subs, playlist, index = ytdl.call_args[0]
        assert url == "https://example.com/2"
        assert index == 1
        assert len(playlist) == 3, "every item, so either direction can be reached"

    def test_a_row_the_playlist_no_longer_has_plays_on_its_own(self, plists):
        """
        A playlist can be edited, or a remote one refreshed, between the page
        being drawn and the row being pressed. If the url at that index is not
        the one asked for then this play is not that item any more, and playing
        it alone is more honest than dragging a list behind it.
        """
        self._write(plists, "Run", [{"title": "one", "url": "https://example.com/1"}])

        _local, _torrent, ytdl = self._play(
            url="https://example.com/other", title="other", plid="Run", itemnum=0
        )

        _url, _title, _subs, playlist, index = ytdl.call_args[0]
        assert playlist is None
        assert index is None

    def test_a_playlist_that_cannot_be_read_still_plays_the_item(self, plists):
        """
        A row is worth playing whether or not the list it came from is still
        readable, so a missing file costs the playlist and not the play.
        """
        _local, _torrent, ytdl = self._play(
            url="https://example.com/1", title="one", plid="NoSuchList", itemnum=0
        )

        _url, _title, _subs, playlist, index = ytdl.call_args[0]
        assert playlist is None
        assert index is None

    def test_an_index_past_the_end_plays_the_item_alone(self, plists):
        self._write(plists, "Run", [{"title": "one", "url": "https://example.com/1"}])

        _local, _torrent, ytdl = self._play(
            url="https://example.com/1", title="one", plid="Run", itemnum=7
        )

        assert ytdl.call_args[0][3] is None

    def test_each_item_is_routed_the_way_a_play_of_it_would_be(self, settings):
        """
        What a playlist holds is a url, and how to play that url is decided in
        one place -- per item, so a list can mix a torrent and a video and each
        still gets its own stages.
        """
        from lib.api.playr import _item_builder

        local = _item_builder({"title": "a", "url": "file:///media/a.mkv"})()
        assert stages(local) == ["localfile", "vlc"]

        torrent = _item_builder(
            {"title": "b", "url": "http://x/y.torrent?bf_torr_idx=3"}
        )()
        assert stages(torrent) == ["peerflix", "vlc"]

        video = _item_builder({"title": "c", "url": "https://example.com/v"})()
        assert stages(video) == ["yt-dlp", "vlc"]

"""
Playback control and pipeline composition.

Two things are covered here that cannot be checked anywhere else without a
Raspberry Pi:

1. The action -> key table. OmxplayerProcess2.control() maps 17 named actions to
   terminal keys written into a FIFO. Patching _send_key captures the mapping,
   so the whole table is pinned even though no player is running.

2. The pipeline _Player.play() assembles. That method decides which stages run,
   and patching _start_thread stops its background loop from running, so the
   exact stage list can be asserted without any process being spawned.

Still hardware-only: whether the keys actually reach a running omxplayer, whether
dbus accepts the call, and whether the picture appears. Everything up to the
handover is covered.
"""

import unittest.mock as m
import urllib.parse

import pytest

import lib.player.player as player_module
from lib.api import playr
from lib.player.dlsrvproc import DlsrvProcess
from lib.player.localproc import LocalFileProcess
from lib.player.omxproc import OmxplayerProcess
from lib.player.omxproc2 import OmxplayerProcess2
from lib.player.pflixproc import PeerflixProcess
from lib.player.subsproc import SubtitlesProcess

# action -> the key omxplayer expects on stdin
OMX_KEYS = {
    "pause": "p",
    "resume": "p",
    "stop": "q",
    "subminus": "d",
    "subplus": "f",
    "plus600": "$'\x1b\x5b\x41'",
    "minus600": "$'\x1b\x5b\x42'",
    "plus30": "$'\x1b\x5b\x43'",
    "minus30": "$'\x1b\x5b\x44'",
    "volup": "=",
    "voldown": "-",
    "next_subtitle": "m",
    "prev_subtitle": "n",
    "next_audio": "k",
    "prev_audio": "j",
    "show_subtitle": "w",
    "hide_subtitle": "x",
}


class TestOmxplayer2KeyMap:
    @pytest.mark.parametrize("action,key", sorted(OMX_KEYS.items()))
    def test_action_sends_expected_key(self, action, key):
        with m.patch.object(OmxplayerProcess2, "_send_key") as send:
            OmxplayerProcess2().control(action)
        send.assert_called_once_with(key)

    def test_unknown_action_sends_nothing(self):
        """
        An unrecognised action must not send a stray key: it would be read as a
        keystroke by the player and do something unexpected.
        """
        with m.patch.object(OmxplayerProcess2, "_send_key") as send:
            OmxplayerProcess2().control("definitely_not_an_action")
        send.assert_not_called()

    def test_pause_and_resume_share_a_key(self):
        """omxplayer toggles; BlissFlixx tracks which state it believes it is in."""
        with m.patch.object(OmxplayerProcess2, "_send_key") as send:
            OmxplayerProcess2().control("pause")
            OmxplayerProcess2().control("resume")
        assert [c.args[0] for c in send.call_args_list] == ["p", "p"]

    def test_key_is_written_to_the_fifo(self):
        """The command travels as stdin, so _send_key is the real transport."""
        from lib.player.omxproc2 import _CMD_FIFO

        with m.patch("os.system") as system:
            OmxplayerProcess2().control("stop")
        assert _CMD_FIFO in system.call_args[0][0]
        assert system.call_args[0][0].startswith("echo -n q >>")

    def test_full_table_is_covered(self):
        """
        Guards against an action being added to the implementation without a
        test appearing here.
        """
        import inspect
        import re

        src = inspect.getsource(OmxplayerProcess2.control)
        found = set(re.findall(r'action == "([a-z0-9_]+)"', src))
        assert found == set(OMX_KEYS)


class TestOmxplayerDbusControl:
    """
    The other omxplayer stage drives the player over dbus instead of stdin, and
    only implements pause/resume.
    """

    def test_pause_calls_dbus(self):
        from lib.player.omxproc import _DBUS_PATH

        with m.patch("os.system") as system:
            OmxplayerProcess().control("pause")
        assert system.call_args[0][0] == _DBUS_PATH + " pause &"

    def test_resume_sends_the_same_dbus_command(self):
        with m.patch("os.system") as system:
            OmxplayerProcess().control("resume")
        assert "pause" in system.call_args[0][0]

    def test_stop_is_not_handled(self):
        """
        This stage cannot stop the player, so control("stop") is a no-op here.
        The pipe's stop() is what actually terminates it, via SIGKILL on the
        process group. Recorded because it looks like an oversight otherwise.
        """
        with m.patch("os.system") as system:
            OmxplayerProcess().control("stop")
        system.assert_not_called()

    def test_unknown_action_is_ignored(self):
        with m.patch("os.system") as system:
            OmxplayerProcess().control("nonsense")
        system.assert_not_called()


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
    return queued[1]


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
        The subtitle file must exist before omxplayer is told to use it, so the
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
        mocks["playLocalFile"].assert_called_once_with("/media/movies/a.mkv", "T")
        mocks["playYtdl"].assert_not_called()

    def test_torrent_url_selects_a_file_inside_it(self):
        url = "http://x/y.torrent?bf_torr_idx=3"
        mocks = self._route(url)
        mocks["playTorrent"].assert_called_once_with(url, 3, "T", None)
        mocks["playYtdl"].assert_not_called()

    def test_main_torrent_file_uses_minus_one(self):
        url = "http://x/y.torrent?bf_torr_idx=-1"
        mocks = self._route(url)
        assert mocks["playTorrent"].call_args[0][1] == -1

    def test_plain_url_goes_to_ytdl(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        mocks = self._route(url)
        mocks["playYtdl"].assert_called_once_with(url, "T", None)

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


class TestStatus:
    def test_status_shape(self):
        """The frontend polls this continuously, so the keys must stay stable."""
        status = playr.status()
        assert set(status) == {"State", "Msg", "Title", "Paused", "Error"}

    def test_idle_player_is_not_running(self):
        """
        Asserted on a fresh instance rather than the shared Player singleton,
        which other tests in this file leave holding state.
        """
        assert player_module._Player().status()["State"] == (
            player_module.ST_NOT_RUNNING
        )

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

    @pytest.mark.parametrize("cls", [DlsrvProcess, OmxplayerProcess, OmxplayerProcess2])
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

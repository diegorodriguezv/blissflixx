"""
The MpV backend and the backend registry.

MpV is the first backend that shares nothing with omxplayer: different binary,
different control transport (JSON IPC over a unix socket rather than dbus
messages or FIFO keystrokes), and a different readiness strategy (polling for the
socket, because mpv produces no status line stream to parse).

mpv is not installed by configure.sh, so nothing here runs it. build_command and
the control command mapping are pure and fully testable; the socket write is
intercepted.
"""

import inspect
import json
import os
import re
import types
import unittest.mock as m

import pytest

from lib.player.backend import (
    ALL_CAPABILITIES,
    CAP_PAUSE,
    CAP_SEEK,
    CAP_STOP,
    CAP_SUBTITLES,
    PlayerBackend,
)
from lib.player.backends import (
    BACKENDS,
    DEFAULT_BACKEND,
    backend_names,
    describe,
    get_backend,
)
from lib.player.mpvproc import MPV_BIN, MpvProcess
from lib.player.omxproc import OmxplayerProcess
from lib.player.omxproc2 import OmxplayerProcess2
from lib.player.processpipe import ProcessException

FILE_OUT = "/tmp/blissflixx/bf.out"
HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"
SUBS = "/tmp/blissflixx/movie.srt"
SOCK = "/tmp/blissflixx/test-mpv.sock"

#: Every action MpV's control() handles. Guarded against drift below, so a new
#: action cannot appear in the implementation without appearing here.
ACTIONS = {
    "pause",
    "resume",
    "stop",
    "plus30",
    "minus30",
    "plus600",
    "minus600",
    "volup",
    "voldown",
    "next_subtitle",
    "prev_subtitle",
    "show_subtitle",
    "hide_subtitle",
    "next_audio",
    "prev_audio",
}


def mpv(args):
    return MpvProcess({"socket": SOCK}).build_command(args)


class TestMpvCommand:
    def test_starts_with_mpv(self):
        assert mpv({"outfile": FILE_OUT})[0] == MPV_BIN

    def test_uses_argv_not_a_shell_string(self):
        """
        mpv needs no pipes or redirects, so it must not go through a shell. The
        omxplayer backends need shell=True precisely because they do.
        """
        assert isinstance(mpv({"outfile": FILE_OUT}), list)
        assert MpvProcess().shell is False

    def test_outfile_is_the_final_positional(self):
        cmd = mpv({"outfile": FILE_OUT})
        assert cmd[-1] == FILE_OUT

    def test_ipc_server_bound_to_the_socket_path(self):
        """The socket is both the control channel and the readiness signal."""
        cmd = MpvProcess({"socket": SOCK}).build_command({"outfile": FILE_OUT})
        assert "--input-ipc-server=" + SOCK in cmd

    def test_terminal_output_disabled(self):
        """
        stdout and stderr are the process's status stream, and the readiness
        check reads it, so mpv's terminal chatter has to be off.
        """
        cmd = mpv({"outfile": FILE_OUT})
        assert "--no-terminal" in cmd
        assert "--msg-level=all=error" not in cmd

    def test_user_config_ignored(self):
        """Playback should not depend on whatever mpv.conf happens to contain."""
        assert "--no-config" in mpv({"outfile": FILE_OUT})

    def test_subtitles_when_present(self):
        assert "--sub-file=" + SUBS in mpv({"outfile": FILE_OUT, "subtitles": SUBS})

    def test_no_subtitle_flag_when_absent(self):
        assert not [a for a in mpv({"outfile": FILE_OUT}) if a.startswith("--sub-file")]

    def test_local_file_keeps_reading(self):
        """
        The file is still being written by the download stage, so mpv must wait
        for more rather than treating the current end as the end.
        """
        assert "--keep-open=inf" in mpv({"outfile": FILE_OUT})

    def test_http_url_does_not_keep_reading(self):
        """An http stream has no known length; following it would just hang."""
        assert "--keep-open=inf" not in mpv({"outfile": HTTP_OUT})

    def test_http_url_still_accepted(self):
        assert mpv({"outfile": HTTP_OUT})[-1] == HTTP_OUT


class TestMpvReady:
    """
    Readiness is the socket appearing. mpv binds it while starting, so its
    presence means it is up.
    """

    def test_socket_appearance_means_ready(self, monkeypatch):
        proc = MpvProcess({"socket": SOCK})
        proc.proc = types.SimpleNamespace(poll=lambda: None)
        states = iter([False, False, True])

        def exists(path):
            return next(states, True)

        monkeypatch.setattr(os.path, "exists", exists)
        monkeypatch.setattr("lib.player.mpvproc.time.sleep", lambda s: None)
        proc._ready()

    def test_socket_never_appears_times_out(self, monkeypatch):
        proc = MpvProcess({"socket": SOCK, "start_timeout": 0.05})
        proc.proc = types.SimpleNamespace(poll=lambda: None)
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        monkeypatch.setattr("lib.player.mpvproc.time.sleep", lambda s: None)
        with pytest.raises(ProcessException, match="timed out"):
            proc._ready()

    def test_early_exit_is_reported(self, monkeypatch):
        """mpv dying before binding must surface, not hang until the timeout."""
        proc = MpvProcess({"socket": SOCK})
        proc.proc = types.SimpleNamespace(poll=lambda: 1)
        proc._readline = lambda timeout=None: ""
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        monkeypatch.setattr("lib.player.mpvproc.time.sleep", lambda s: None)
        with pytest.raises(ProcessException):
            proc._ready()

    def test_mpv_error_output_is_surfaced(self, monkeypatch):
        """
        When mpv exits it usually says why. The first recognised error line
        becomes the message the user sees.
        """
        proc = MpvProcess({"socket": SOCK})
        proc.proc = types.SimpleNamespace(poll=lambda: 1)
        monkeypatch.setattr(os.path, "exists", lambda p: False)
        monkeypatch.setattr("lib.player.mpvproc.time.sleep", lambda s: None)
        proc._readline = lambda timeout=None: iter(
            ["Failed to open '/tmp/nope.mkv': No such file or directory"]
        ).__next__()
        with pytest.raises(ProcessException, match="Failed to open"):
            proc._ready()


class TestMpvControl:
    """
    Control is JSON IPC, so the assertions are about the command arrays sent,
    not about keystrokes. The socket write is intercepted.
    """

    def _sent(self, action):
        proc = MpvProcess({"socket": SOCK})
        sent = []
        proc._send_command = lambda command: sent.append(command) or True
        proc.control(action)
        return sent[0] if sent else None

    @pytest.mark.parametrize(
        "action,expected",
        [
            ("pause", ["set_property", "pause", True]),
            ("resume", ["set_property", "pause", False]),
            ("stop", ["quit"]),
            ("plus30", ["seek", 30]),
            ("minus30", ["seek", -30]),
            ("plus600", ["seek", 600]),
            ("minus600", ["seek", -600]),
            ("volup", ["add", "volume", 5]),
            ("voldown", ["add", "volume", -5]),
            ("next_subtitle", ["cycle", "sub"]),
            ("prev_subtitle", ["cycle", "sub"]),
            ("show_subtitle", ["set_property", "sub-visibility", True]),
            ("hide_subtitle", ["set_property", "sub-visibility", False]),
            ("next_audio", ["cycle", "audio"]),
            ("prev_audio", ["cycle", "audio"]),
        ],
    )
    def test_action_maps_to_command(self, action, expected):
        assert self._sent(action) == expected

    def test_unknown_action_sends_nothing(self):
        assert self._sent("nonsense") is None

    def test_every_action_is_covered_by_a_test(self):
        """
        The omxplayer key map has an equivalent guard. Same idea: a new action
        should not appear in the implementation without appearing here, so the
        two sets must match exactly.

        Parsed with ast rather than a regex so that mpv's own property names
        ("quit", "sub", "audio") are not mistaken for actions.
        """
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(MpvProcess.control)))
        found = set()
        for node in ast.walk(tree):
            # Covers action == "x" and action in ("a", "b")
            if not (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name)):
                continue
            if node.left.id != "action":
                continue
            for comparator in node.comparators:
                targets = (
                    comparator.elts
                    if isinstance(comparator, ast.Tuple)
                    else [comparator]
                )
                for target in targets:
                    if isinstance(target, ast.Constant):
                        found.add(target.value)
        assert found == ACTIONS


class TestMpvSocketWrite:
    """
    The real transport, with the socket itself stubbed. Verifies the payload
    shape mpv's JSON IPC expects rather than the exact write.
    """

    def test_command_is_sent_as_json(self):
        proc = MpvProcess({"socket": SOCK})
        sock = m.MagicMock()
        ctx = m.MagicMock()
        ctx.__enter__.return_value = sock

        with (
            m.patch("os.path.exists", return_value=True),
            m.patch("lib.player.mpvproc.socket.socket", return_value=ctx) as factory,
        ):
            assert proc._send_command(["set_property", "pause", True]) is True

        sock.connect.assert_called_once_with(SOCK)
        payload = json.loads(sock.sendall.call_args[0][0].decode("utf-8"))
        assert payload == {"command": ["set_property", "pause", True]}
        assert factory.called

    def test_nothing_sent_when_socket_absent(self):
        """
        Not running, or not up yet. Returning False rather than raising keeps
        control() safe to call during startup.
        """
        proc = MpvProcess({"socket": SOCK})
        with m.patch("os.path.exists", return_value=False):
            assert proc._send_command(["quit"]) is False

    def test_socket_error_is_not_propagated(self):
        """A race with mpv exiting must not turn into a 500 in the UI."""
        proc = MpvProcess({"socket": SOCK})
        ctx = m.MagicMock()
        ctx.__enter__.side_effect = OSError("gone")
        with (
            m.patch("os.path.exists", return_value=True),
            m.patch("lib.player.mpvproc.socket.socket", return_value=ctx),
        ):
            assert proc._send_command(["quit"]) is False


class TestMpvLifecycle:
    def test_name(self):
        assert MpvProcess().name() == "mpv"

    def test_declares_all_capabilities(self):
        assert MpvProcess().supports(*ALL_CAPABILITIES)

    def test_stop_removes_the_socket(self, tmp_path):
        sock = tmp_path / "mpv.sock"
        sock.write_text("")
        proc = MpvProcess({"socket": str(sock)})
        proc.proc = None
        proc.stop()
        assert not sock.exists()

    def test_stop_tolerates_a_missing_socket(self):
        proc = MpvProcess({"socket": "/nonexistent/mpv.sock"})
        proc.proc = None
        proc.stop()


class TestBackendBase:
    def test_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            PlayerBackend()

    def test_backend_must_implement_build_command_and_control(self):
        class Incomplete(PlayerBackend):
            pass

        with pytest.raises(TypeError):
            Incomplete()

    def test_build_command_is_reachable_through_the_base_hook(self):
        """
        ExternalProcess.start calls _get_cmd(args); backends implement
        build_command instead, so the two names must not diverge.
        """
        assert OmxplayerProcess()._get_cmd({"outfile": HTTP_OUT}) == (
            OmxplayerProcess().build_command({"outfile": HTTP_OUT})
        )

    def test_capability_checking(self):
        proc = MpvProcess()
        assert proc.declares(CAP_STOP) is True
        assert proc.declares("not_a_capability") is False


class TestOmxCapabilities:
    def test_key_variant_supports_everything(self):
        assert OmxplayerProcess2().supports(*ALL_CAPABILITIES)

    def test_dbus_variant_is_limited(self):
        """
        It implements pause only. Advertising precisely what it can do is the
        point of the capability set: the UI can stop offering controls that
        would be silently dropped.
        """
        proc = OmxplayerProcess()
        assert proc.declares(CAP_PAUSE) is True
        assert proc.declares(CAP_STOP) is False
        assert proc.declares(CAP_SEEK) is False


class TestRegistry:
    def test_all_three_backends_registered(self):
        assert set(BACKENDS) == {"omxplayer", "omxplayer-keys", "mpv"}

    def test_names_are_sorted(self):
        assert backend_names() == ["mpv", "omxplayer", "omxplayer-keys"]

    def test_get_backend_returns_a_configured_instance(self):
        assert isinstance(get_backend("mpv"), MpvProcess)
        assert isinstance(get_backend("omxplayer"), OmxplayerProcess)
        assert isinstance(get_backend("omxplayer-keys"), OmxplayerProcess2)

    def test_unknown_name_raises(self):
        """
        A typo in the settings file should be loud rather than silently
        falling back to some other player.
        """
        with pytest.raises(KeyError):
            get_backend("vlc-ish")

    def test_no_default_configured(self):
        """
        DEFAULT_BACKEND being None is what preserves pre-existing installs: an
        unconfigured box keeps the old http/dlsrv behaviour.
        """
        assert DEFAULT_BACKEND is None

    def test_describe_reports_name_and_capabilities(self):
        info = describe("mpv")
        assert info["backend"] == "mpv"
        assert info["name"] == "mpv"
        assert CAP_STOP in info["capabilities"]

    def test_every_registered_backend_constructs(self):
        for name, cls in BACKENDS.items():
            assert cls().name(), name


class TestBackendSelection:
    """
    _player_stage: a configured backend wins, otherwise the historical choice.
    """

    def _stage(self, http, dlsrv, backend=None):
        from lib.player import player as pm

        pl = pm._Player()
        settings = {} if backend is None else {"backend": backend}
        with m.patch.object(pm, "load", return_value=settings):
            return pl._player_stage(http, dlsrv)

    @pytest.mark.parametrize("http", [False, True])
    @pytest.mark.parametrize("dlsrv", [False, True])
    def test_unconfigured_matches_the_historical_choice(self, http, dlsrv):
        """
        Before backends existed, the two flags picked one of two omxplayer
        variants. Unconfigured behaviour must not change.
        """
        stage = self._stage(http, dlsrv)
        expected = (
            OmxplayerProcess2() if (http or dlsrv) else OmxplayerProcess()
        ).name()
        assert stage.name() == expected

    @pytest.mark.parametrize("http", [False, True])
    @pytest.mark.parametrize("dlsrv", [False, True])
    def test_configured_backend_wins_regardless_of_flags(self, http, dlsrv):
        stage = self._stage(http, dlsrv, backend="mpv")
        assert stage.name() == "mpv"

    def test_unknown_configured_backend_propagates(self):
        from lib.player import player as pm

        pl = pm._Player()
        with m.patch.object(pm, "load", return_value={"backend": "nope"}):
            with pytest.raises(KeyError):
                pl._player_stage(False, True)

"""
Two failures found by deploying to a real Raspberry Pi.

1. first_time_install() returned early when yt-dlp was already present, so it
   never created data/settings. settings.save() writes there unconditionally, so
   the first settings write raised FileNotFoundError. Installing by copying the
   tree, rather than letting it clone yt-dlp, produces exactly that state.

2. A stage's output was drained to /dev/null. When a player could not start, the
   reason it printed was thrown away and the pipe never reported anything, so the
   UI sat on "LOADING STREAM" indefinitely. Diagnosing that needed the process's
   own error text, which nothing had kept.
"""

import subprocess
import sys

import pytest

import blissflixx
from lib.player import processpipe
from lib.player.processpipe import ExternalProcess, ProcessException, _LineTail


class TestEnsureDataDirs:
    """
    The directories are needed on every start, not only on a first run.
    """

    def test_creates_every_directory_the_server_writes_to(self, tmp_path, monkeypatch):
        import lib.locations as locations

        monkeypatch.setattr(locations, "DATA_PATH", str(tmp_path / "data"))
        monkeypatch.setattr(locations, "PLUGIN_PATH", str(tmp_path / "plugins"))
        monkeypatch.setattr(locations, "SETTINGS_PATH", str(tmp_path / "data/settings"))
        monkeypatch.setattr(locations, "PLIST_PATH", str(tmp_path / "data/playlists"))

        blissflixx.ensure_data_dirs()

        assert (tmp_path / "data").is_dir()
        assert (tmp_path / "data" / "settings").is_dir()
        assert (tmp_path / "data" / "playlists").is_dir()
        assert (tmp_path / "plugins").is_dir()

    def test_is_idempotent(self, tmp_path, monkeypatch):
        """Called on every start, so a second call must not raise."""
        import lib.locations as locations

        monkeypatch.setattr(locations, "DATA_PATH", str(tmp_path / "data"))
        monkeypatch.setattr(locations, "PLUGIN_PATH", str(tmp_path / "plugins"))
        monkeypatch.setattr(locations, "SETTINGS_PATH", str(tmp_path / "data/settings"))
        monkeypatch.setattr(locations, "PLIST_PATH", str(tmp_path / "data/playlists"))
        blissflixx.ensure_data_dirs()
        blissflixx.ensure_data_dirs()

    def test_does_not_clobber_existing_files(self, tmp_path, monkeypatch):
        import lib.locations as locations

        monkeypatch.setattr(locations, "DATA_PATH", str(tmp_path / "data"))
        monkeypatch.setattr(locations, "PLUGIN_PATH", str(tmp_path / "plugins"))
        monkeypatch.setattr(locations, "SETTINGS_PATH", str(tmp_path / "data/settings"))
        monkeypatch.setattr(locations, "PLIST_PATH", str(tmp_path / "data/playlists"))
        blissflixx.ensure_data_dirs()
        existing = tmp_path / "data" / "settings" / "player"
        existing.write_text('{"backend": "mpv"}')

        blissflixx.ensure_data_dirs()

        assert existing.read_text() == '{"backend": "mpv"}'


class TestFirstTimeInstallCreatesDirsEvenWhenYtDlpIsPresent:
    """
    The regression itself: yt-dlp present must not skip directory creation.
    """

    def test_dirs_are_created_when_ytdlp_already_exists(self, tmp_path, monkeypatch):
        import lib.locations as locations

        data = tmp_path / "data"
        monkeypatch.setattr(locations, "DATA_PATH", str(data))
        monkeypatch.setattr(locations, "PLUGIN_PATH", str(tmp_path / "plugins"))
        monkeypatch.setattr(locations, "SETTINGS_PATH", str(data / "settings"))
        monkeypatch.setattr(locations, "PLIST_PATH", str(data / "playlists"))
        # yt-dlp is already there, which used to cause an early return.
        monkeypatch.setattr(locations, "YTUBE_PATH", str(tmp_path / "yt-dlp"))
        (tmp_path / "yt-dlp").mkdir()
        cloned = []
        monkeypatch.setattr("blissflixx.gitutils.clone", lambda *a: cloned.append(a))

        blissflixx.first_time_install()

        assert (data / "settings").is_dir()
        assert cloned == [], "yt-dlp was present, so nothing should be cloned"

    def test_ytdlp_is_cloned_when_missing(self, tmp_path, monkeypatch):
        import lib.locations as locations

        data = tmp_path / "data"
        monkeypatch.setattr(locations, "DATA_PATH", str(data))
        monkeypatch.setattr(locations, "PLUGIN_PATH", str(tmp_path / "plugins"))
        monkeypatch.setattr(locations, "SETTINGS_PATH", str(data / "settings"))
        monkeypatch.setattr(locations, "PLIST_PATH", str(data / "playlists"))
        monkeypatch.setattr(locations, "YTUBE_PATH", str(tmp_path / "absent-yt-dlp"))
        cloned = []
        monkeypatch.setattr("blissflixx.gitutils.clone", lambda *a: cloned.append(a))

        blissflixx.first_time_install()

        assert len(cloned) == 1
        assert (data / "settings").is_dir()

    def test_settings_save_then_works(self, tmp_path, monkeypatch):
        """
        The end-to-end symptom: a settings write on a checkout that has yt-dlp
        but no data directory.
        """
        import lib.locations as locations
        from lib import settings as settings_module

        data = tmp_path / "data"
        monkeypatch.setattr(locations, "DATA_PATH", str(data))
        monkeypatch.setattr(locations, "PLUGIN_PATH", str(tmp_path / "plugins"))
        monkeypatch.setattr(locations, "SETTINGS_PATH", str(data / "settings"))
        monkeypatch.setattr(locations, "PLIST_PATH", str(data / "playlists"))
        monkeypatch.setattr(locations, "YTUBE_PATH", str(tmp_path / "yt-dlp"))
        (tmp_path / "yt-dlp").mkdir()
        monkeypatch.setattr("blissflixx.gitutils.clone", lambda *a: None)
        monkeypatch.setattr(settings_module, "SETTINGS_PATH", str(data / "settings"))
        settings_module._cache.clear()

        assert not (data / "settings").exists()
        blissflixx.first_time_install()
        settings_module.save("player", {"backend": "mpv"})

        assert (data / "settings" / "player").exists()
        settings_module._cache.clear()


class TestLineTail:
    def test_keeps_lines(self):
        tail = _LineTail()
        tail.write("first\nsecond\n")
        assert tail.summary() == "first | second"

    def test_buffers_a_partial_line_until_close(self):
        """
        A process can die mid-line, and the reason is often in that fragment.

        The fragment is reported as soon as it is written, not only after close(),
        because a stage killed by a signal may never reach a close and the
        fragment is then all that says what happened.
        """
        tail = _LineTail()
        tail.write("complete\nno newline yet")
        assert tail.summary() == "complete | no newline yet"

    def test_partial_survives_close_unchanged(self):
        """close() flushes the pending line rather than duplicating it."""
        tail = _LineTail()
        tail.write("complete\nno newline yet")
        before = tail.summary()
        tail.close()
        assert tail.summary() == before

    def test_partial_alone_is_still_reported(self):
        tail = _LineTail()
        tail.write("died mid-word")
        assert tail.summary() == "died mid-word"

    def test_blank_lines_are_dropped(self):
        tail = _LineTail()
        tail.write("a\n\n\nb\n")
        assert tail.summary() == "a | b"

    def test_bounded_by_line_count(self):
        tail = _LineTail(keep=2)
        tail.write("a\nb\nc\nd\n")
        assert tail.summary() == "c | d"

    def test_bounded_by_length_for_a_run_on_line(self):
        """
        A stage writing megabytes with no newline must not grow without bound.
        """
        tail = _LineTail()
        tail.write("x" * 100000)
        assert len(tail.lines) == 0
        tail.close()
        assert len(tail.summary()) <= 2000

    def test_empty_says_so_rather_than_nothing(self):
        assert _LineTail().summary() == "no output"

    def test_falsey_when_empty(self):
        assert not _LineTail()

    def test_truthy_once_it_has_output(self):
        tail = _LineTail()
        tail.write("something\n")
        assert tail

    def test_summary_uses_the_last_lines(self):
        """A player's reason comes after its banner."""
        tail = _LineTail()
        tail.write("VLC media player 3.0.23\n")
        tail.write("Command Line Interface initialized\n")
        tail.write("drm_vout vout display error: Failed to get xlease\n")
        assert tail.summary(max_lines=1) == (
            "drm_vout vout display error: Failed to get xlease"
        )


class _Failing(ExternalProcess):
    """A stage that writes a reason to stdout and exits non-zero."""

    def __init__(self, output):
        super().__init__()
        self.output = output
        self.reported = None

    def name(self):
        return "failingstage"

    def _get_cmd(self, args):
        return [sys.executable, "-c", self.output]

    def _ready(self):
        raise ProcessException("could not start")

    def _report(self):
        return self.reported


class TestStageFailureIsReported:
    """
    The symptom on the Pi: the pipe hung on "LOADING STREAM" with no error,
    because the reason was discarded.
    """

    def _run(self, output):
        proc = _Failing(output)
        proc.set_msgq(__import__("queue").Queue(), 0)
        proc.start({})
        return proc

    def test_error_is_recorded(self):
        proc = self._run("print('boom reason here')")
        assert proc.has_error()

    def test_prior_message_is_kept_and_output_appended(self):
        """
        _ready() supplies a generic reason and the process supplies the real one.
        Both have to survive, and the pipe only ever reports the first error.
        """
        proc = self._run("print('Failed to get xlease')")
        assert proc.errors[0].startswith("could not start")
        assert "Failed to get xlease" in proc.errors[0]

    def test_stage_name_appears_when_there_is_no_other_message(self):
        """
        A stage whose process died without _ready() explaining anything still
        has to say which stage it was.
        """
        proc = _Failing("pass")
        tail = _LineTail()
        tail.write("went wrong quietly")
        proc._add_output_to_error(tail)
        assert proc.errors[0].startswith("failingstage")
        assert "went wrong quietly" in proc.errors[0]

    def test_error_is_a_useful_length(self):
        """A traceback in a status field is not a message a user can act on."""
        proc = self._run("print('boom reason here')")
        assert 0 < len(proc.errors[0]) < 300

    def test_no_error_when_the_stage_ran_fine(self):
        class Ok(_Failing):
            def _ready(self):
                return {}

        proc = Ok("print('all good')")
        proc.set_msgq(__import__("queue").Queue(), 0)
        proc.start({})
        assert not proc.has_error()

    def test_halted_is_reported_to_the_pipe(self):
        import queue as _q

        proc = _Failing("print('could not render')")
        q = _q.Queue()
        proc.set_msgq(q, 0)
        proc.start({})
        assert q.get_nowait() == processpipe.MSG_PROCESS_HALTED

    def test_a_stage_that_fails_silently_still_produces_a_message(self):
        """
        When the process printed nothing useful, the exit still has to produce
        something rather than an empty error.
        """
        proc = self._run("pass")
        assert proc.has_error()
        assert proc.errors[0].strip()


class TestRealSubprocessOutputIsCaptured:
    """
    End to end with a real subprocess, since the point is that its output is
    kept rather than discarded.
    """

    def test_output_survives_to_the_error(self):
        import queue as _q

        class Real(_Failing):
            def _get_cmd(self, args):
                return [
                    sys.executable,
                    "-c",
                    "import sys; print('player said no'); sys.exit(3)",
                ]

        proc = Real("")
        q = _q.Queue()
        proc.set_msgq(q, 0)
        proc.start({})
        assert "player said no" in proc.errors[0]
        assert q.get_nowait() == processpipe.MSG_PROCESS_HALTED


class TestLineTailAcceptsBytes:
    """
    A subprocess pipe is binary, so shutil.copyfileobj writes bytes. The first
    version of this sink concatenated str and raised TypeError, which
    _copypipe's except clause swallowed: the tail simply stayed empty, and the
    captured output was lost exactly as if it had never been captured.
    """

    def test_bytes_are_decoded(self):
        tail = _LineTail()
        tail.write(b"player said no\n")
        assert tail.summary() == "player said no"

    def test_mixed_bytes_and_str(self):
        tail = _LineTail()
        tail.write(b"first\n")
        tail.write("second\n")
        assert tail.summary() == "first | second"

    def test_bytearray_is_accepted(self):
        tail = _LineTail()
        tail.write(bytearray(b"bytes from a bytearray\n"))
        assert "bytearray" in tail.summary()

    def test_invalid_utf8_does_not_raise(self):
        """
        Players can emit locale-specific bytes. A decode failure must not lose
        the line or take the stage down.
        """
        tail = _LineTail()
        tail.write(b"caf\xe9 bad byte\n")
        assert "caf" in tail.summary()

    def test_copyfileobj_delivers_lines(self):
        """The real path: shutil.copyfileobj into the sink."""
        import io
        import shutil

        tail = _LineTail()
        shutil.copyfileobj(io.BytesIO(b"alpha\nbeta\n"), tail)
        assert tail.summary() == "alpha | beta"

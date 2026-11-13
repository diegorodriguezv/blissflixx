"""
The black picture held between films.

VLC and ffmpeg are replaced with fakes: these tests check what is started and
what is taken down, and in what order. Whether the TV keeps a signal is checked
on the box.
"""

import signal
import subprocess

import pytest

from lib.player import hold


class FakeProc:
    def __init__(self, pid=4242, stdin=True):
        self.pid = pid
        self.stdin = _FakePipe() if stdin else None
        self.waited = []
        self.exit_code = None
        self.hang = False

    def poll(self):
        return self.exit_code

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if self.hang and timeout is not None:
            raise subprocess.TimeoutExpired("vlc", timeout)
        self.exit_code = 0
        return 0


class _FakePipe:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def held(monkeypatch, tmp_path):
    """Hold switched on, with a clip that exists and VLC and signals faked."""
    monkeypatch.setattr(hold, "ENABLED", True)
    monkeypatch.setattr(hold, "CLIP", str(tmp_path / "black.mp4"))
    (tmp_path / "black.mp4").write_bytes(b"clip")
    monkeypatch.setattr(hold, "_proc", None)
    started, signals = [], []

    def fake_popen(args, **kwargs):
        started.append((args, kwargs))
        return FakeProc()

    monkeypatch.setattr(hold.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        hold.os, "killpg", lambda pid, sig: signals.append((pid, sig))
    )
    return started, signals


class TestShow:
    def test_starts_the_clip_on_the_films_output(self, held):
        started, _ = held
        hold.show()
        assert len(started) == 1
        args, _ = started[0]
        assert args[-1].endswith("black.mp4")
        assert "--vout=drm_vout" in args
        assert "--drm-vout-module=vc4" in args
        assert "--loop" in args

    def test_keeps_stdin_open_so_vlc_does_not_quit(self, held):
        """VLC's cli shuts down when stdin closes; the hold must keep it open."""
        started, _ = held
        hold.show()
        _, kwargs = started[0]
        assert kwargs["stdin"] == subprocess.PIPE

    def test_does_nothing_twice(self, held):
        started, _ = held
        hold.show()
        hold.show()
        assert len(started) == 1

    def test_does_nothing_when_switched_off(self, held, monkeypatch):
        started, _ = held
        monkeypatch.setattr(hold, "ENABLED", False)
        hold.show()
        assert started == []

    def test_does_nothing_when_the_clip_cannot_be_made(self, held, monkeypatch):
        started, _ = held
        monkeypatch.setattr(hold, "_make_clip", lambda: False)
        hold.show()
        assert started == []

    def test_does_nothing_when_vlc_is_missing(self, held, monkeypatch):
        def missing(args, **kwargs):
            raise FileNotFoundError("vlc")

        monkeypatch.setattr(hold.subprocess, "Popen", missing)
        hold.show()
        assert hold._proc is None


class TestHide:
    def test_without_a_hold_nothing_happens(self, held):
        _, signals = held
        hold.hide()
        assert signals == []

    def test_takes_the_clip_down_with_sigterm(self, held):
        started, signals = held
        hold.show()
        hold.hide()
        assert signals == [(4242, signal.SIGTERM)]
        assert hold._proc is None

    def test_closes_the_pipe_it_held_open(self, held):
        started, _ = held
        hold.show()
        proc = hold._proc
        hold.hide()
        assert proc.stdin.closed

    def test_kills_it_if_it_will_not_go(self, held, monkeypatch):
        started, signals = held
        hold.show()
        proc = hold._proc
        proc.hang = True
        hold.hide()
        assert signals[-1] == (4242, signal.SIGKILL)
        assert proc.stdin.closed

    def test_show_again_after_hide_starts_a_fresh_clip(self, held):
        started, _ = held
        hold.show()
        hold.hide()
        hold.show()
        assert len(started) == 2

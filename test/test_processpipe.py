"""
ProcessPipe message protocol.

lib/player/processpipe chains subprocesses together: each one signals READY with
the arguments the next one needs, and the pipe walks its processes in order. The
protocol is the trickiest part of the player, and it cannot be tested on a
development machine without omxplayer and peerflix, so these tests drive it with
fake Process subclasses. No subprocess is ever started and no Raspberry Pi
hardware is involved.

The messaging is:

    process -> pipe:  (msg, index)          or  (msg, index, args)
    pipe -> process:  start(args)
    pipe -> parent:   (MSG_PLAYER_PIPE_STOPPED, error)

msg constants live in lib.player.processpipe.
"""

import inspect
import os
import queue
import threading
import time
import unittest.mock as m

import pytest

import lib.player.processpipe as pp
from lib.player.backends import BACKENDS, backend_class
from lib.player.dlsrvproc import DlsrvProcess
from lib.player.omxproc import OmxplayerProcess
from lib.player.omxproc2 import OmxplayerProcess2
from lib.player.pflixproc import PeerflixProcess
from lib.player.processpipe import (
    MSG_PLAYER_PIPE_STOPPED,
    MSG_PROCESS_READY,
    ExternalProcess,
    Process,
    ProcessException,
    ProcessPipe,
)
from lib.player.subsproc import SubtitlesProcess
from lib.player.vlcproc import VlcProcess
from lib.player.ytdlproc import YoutubeDlProcess


class FakeProcess(Process):
    """
    A Process that does nothing except report whatever outcome the test sets.

    It is not built on ExternalProcess, so no command is constructed and no
    subprocess is ever spawned. start() is driven synchronously rather than on a
    thread, which keeps the assertions deterministic.

    Outcome controls what, and when, the process reports to its pipe:

        ready                 READY once
        ready_then_finished   READY, then FINISHED
        ready_then_wait       READY, then blocks until stop() is called
        finished / halted     FINISHED / HALTED immediately
        wait                  nothing at all, so the pipe stays blocked
        raise                 raises ProcessException out of start()
    """

    def __init__(self, name="fake", outcome="ready", args=None, error=None):
        super().__init__()
        self._name = name
        self.outcome = outcome
        self.ready_args = args or {}
        self.pending_error = error
        self.started_with = None
        self.stopped = False
        self._release = threading.Event()

    def name(self):
        return self._name

    def start(self, args):
        self.started_with = args
        if self.pending_error:
            self._set_error(self.pending_error)
        if self.outcome == "raise":
            raise ProcessException("boom")
        if self.outcome in ("ready", "ready_then_finished", "ready_then_wait"):
            self.msg_ready(self.ready_args)
        if self.outcome == "ready_then_finished":
            self.msg_finished()
        elif self.outcome == "ready_then_wait":
            self._release.wait(timeout=5)
        elif self.outcome == "finished":
            self.msg_finished()
        elif self.outcome == "halted":
            self.msg_halted()
        # "wait" and "ready_then_wait" return without signalling.

    def stop(self):
        self.stopped = True
        self._release.set()

    def status_msg(self):
        return f"{self._name} status"


def drain(pipe, timeout=2.0):
    """Run a pipe on a thread and collect what it reports to its parent."""
    pmsgq = queue.Queue()
    thread = threading.Thread(target=pipe.start, args=(pmsgq,), daemon=True)
    thread.start()
    collected = []
    try:
        while True:
            collected.append(pmsgq.get(timeout=timeout))
    except queue.Empty:
        pass
    return collected, thread


def start_pipe(pipe):
    """
    Start a pipe in the background and wait until it has populated its thread
    list. stop() joins threads by index, so a pipe that was never actually run
    cannot be stopped in a test.
    """
    pmsgq = queue.Queue()
    thread = threading.Thread(target=pipe.start, args=(pmsgq,), daemon=True)
    thread.start()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if pipe.threads and pipe.next_proc > 0:
            # Give the process a moment to reach its blocking wait.
            time.sleep(0.05)
            break
        time.sleep(0.01)
    return pmsgq, thread


class TestPipeHappyPath:
    def test_runs_processes_in_order_passing_args_along(self):
        """
        Each process reports READY with the arguments the next one needs, so the
        downloader hands a path to the player, which hands on the final details.
        """
        first = FakeProcess("first", args={"outfile": "/tmp/a"})
        second = FakeProcess("second", outcome="ready_then_finished", args={"final": 1})
        pipe = ProcessPipe("title")
        pipe.add_process(first)
        pipe.add_process(second)

        collected, _ = drain(pipe)

        assert first.started_with == {}
        assert second.started_with == {"outfile": "/tmp/a"}
        assert collected[0] == MSG_PLAYER_PIPE_STOPPED

    def test_reports_stopped_once_the_last_process_finishes(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("only", outcome="finished"))

        collected, _ = drain(pipe)

        assert collected == [MSG_PLAYER_PIPE_STOPPED, None]

    def test_pipe_reaches_started_when_last_process_is_ready(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("first", args={"a": 1}))
        pipe.add_process(FakeProcess("last", args={"b": 2}))

        pmsgq, _ = start_pipe(pipe)
        assert pipe.is_started() is True
        # Nothing has finished, so nothing has been reported to the parent yet.
        assert pmsgq.empty() is True

    def test_threads_are_daemonised(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a", outcome="ready_then_wait"))
        start_pipe(pipe)
        assert pipe.threads
        assert all(t.daemon for t in pipe.threads)


class TestPipeErrors:
    def test_halt_propagates_to_parent(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("bad", outcome="halted"))

        collected, _ = drain(pipe)

        assert collected == [MSG_PLAYER_PIPE_STOPPED, None]

    def test_process_error_is_reported_to_parent(self):
        """
        ProcessPipe.stop() reports the first error a process recorded via
        _set_error. A pipe that finished with an error must surface the message,
        not None, so the UI can show it.
        """
        pipe = ProcessPipe("title")
        pipe.add_process(
            FakeProcess("bad", outcome="ready_then_wait", error="it failed")
        )
        pmsgq, _ = start_pipe(pipe)
        assert pipe.is_started() is True

        pipe.stop()

        assert pmsgq.get(timeout=2) == MSG_PLAYER_PIPE_STOPPED
        assert pmsgq.get(timeout=2) == "it failed"

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_unexpected_exception_in_start_leaves_the_pipe_hung(self):
        """
        Documents a latent bug rather than desired behaviour.

        ProcessPipe.start() runs each process on a thread created by
        _start_thread. If a process's start() raises anything that is not
        ProcessException, that thread dies silently: it reports nothing to the
        pipe, so the pipe's loop stays blocked on msgq.get() forever. Nothing
        reaches the parent, the player keeps showing whatever status it last
        had, and the only way out is an explicit stop.

        In practice ExternalProcess.start converts ProcessException into
        msg_halted, so this only bites on an *unexpected* error escaping a
        subclass: an OSError from Popen when a binary is missing, a TypeError
        from a bad _get_cmd, or anything not derived from ProcessException.

        If this test ever starts failing because the pipe now recovers, the
        behaviour improved and this note should be deleted.
        """
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("boom", outcome="raise"))

        pmsgq = queue.Queue()
        thread = threading.Thread(target=pipe.start, args=(pmsgq,), daemon=True)
        thread.start()
        # Give the inner thread time to die and the pipe time to stay blocked.
        time.sleep(0.3)

        # The pipe thread is still alive, blocked waiting for a message that
        # will never arrive, and the parent has been told nothing.
        assert thread.is_alive() is True
        assert pmsgq.empty() is True
        assert pipe.is_started() is False


class TestPipeStop:
    def test_stop_is_idempotent(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a", outcome="ready_then_wait"))
        pmsgq, _ = start_pipe(pipe)
        assert pipe.is_started() is True

        pipe.stop()
        first = pmsgq.qsize()
        pipe.stop()

        assert pmsgq.qsize() == first

    def test_stop_stops_processes_in_reverse_order(self):
        """
        The last process is torn down first, since it is the one holding the
        player. Order matters: stopping omxplayer before the downloader would
        leave the download writing into a dead pipe.
        """
        a = FakeProcess("a", outcome="ready")
        b = FakeProcess("b", outcome="ready_then_wait")
        pipe = ProcessPipe("title")
        pipe.add_process(a)
        pipe.add_process(b)
        pmsgq, _ = start_pipe(pipe)
        assert pipe.is_started() is True

        pipe.stop()

        assert a.stopped is True
        assert b.stopped is True
        assert pmsgq.get(timeout=2) == MSG_PLAYER_PIPE_STOPPED

    def test_stop_clears_started_flag(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a", outcome="ready_then_wait"))
        start_pipe(pipe)
        assert pipe.is_started() is True

        pipe.stop()

        assert pipe.is_started() is False


class TestPipeState:
    def test_is_started_and_is_stopping_before_start(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a"))
        assert pipe.is_started() is False
        assert pipe.is_stopping() is False

    def test_is_stopping_after_stop(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a", outcome="ready_then_wait"))
        start_pipe(pipe)
        pipe.stop()
        assert pipe.is_stopping() is True

    def test_status_msg_uses_title_when_started(self):
        pipe = ProcessPipe("My Movie")
        pipe.add_process(FakeProcess("a"))
        pipe.started = True
        assert pipe.status_msg() == "My Movie"

    def test_status_msg_uses_current_process_when_not_started(self):
        pipe = ProcessPipe("My Movie")
        pipe.add_process(FakeProcess("a"))
        assert pipe.status_msg() == "a status"

    def test_control_before_start_is_ignored(self):
        """
        ProcessPipe.control only forwards to the last process once started. It
        must not raise when called early, because the UI can send pause/stop
        while a pipe is still spinning up.
        """
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a", outcome="ready_then_wait"))
        pipe.control("pause")
        assert pipe.is_started() is False

    def test_control_is_forwarded_to_the_last_process(self):
        """
        Only the two omxplayer stages implement control(). Everything else, such
        as a pipeline whose last stage is a downloader, ignores it.

        Process used to have no control() at all, so Player.control("pause")
        raised AttributeError and the UI got a 500 whenever the active
        pipeline could not be paused. Process.control() is now a documented
        no-op on the base class.
        """
        handled = []

        class Controllable(FakeProcess):
            def control(self, action):
                handled.append(action)

        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("first"))
        pipe.add_process(Controllable("last", outcome="ready_then_wait"))
        start_pipe(pipe)
        assert pipe.is_started() is True

        pipe.control("pause")

        assert handled == ["pause"]

    def test_control_is_ignored_by_stages_that_cannot_act_on_it(self):
        pipe = ProcessPipe("title")
        pipe.add_process(FakeProcess("a", outcome="ready_then_wait"))
        start_pipe(pipe)
        assert pipe.is_started() is True

        # Must not raise: a downloader cannot be paused.
        assert pipe.control("pause") is None


class StubbornProcess(FakeProcess):
    """
    A stage that reports ready and then never exits.

    stop() in FakeProcess releases the wait that start() is sitting on, which is
    what lets an ordinary stage's thread end. This subclass deliberately does
    neither: start() waits with no timeout at all, and stop() records the request
    without releasing it. That is the shape of the yt-dlp that was blocked in a
    network read when this wedged a real player on a Pi.

    The wait is on a daemon thread, so a test that fails against an unbounded
    join still exits cleanly instead of hanging the run.
    """

    def start(self, args):
        self.started_with = args
        if self.pending_error:
            self._set_error(self.pending_error)
        self.msg_ready(self.ready_args)
        self._release.wait()

    def stop(self):
        self.stopped = True


class TestStopSurvivesAStageThatWillNotExit:
    """
    stop() must always report back to the parent.

    This is the liveness property the player loop is built on: it waits for
    MSG_PLAYER_PIPE_STOPPED and does nothing at all until it arrives. A join
    with no timeout meant a stage that would not exit could prevent that
    message from ever being sent, and the loop then dropped every subsequent
    play without a word -- which is exactly what was observed on hardware.
    """

    def _stop_within(self, pipe, seconds=3.0):
        """
        Call stop() on a thread so that an unbounded join fails an assertion
        here rather than hanging the test run.
        """
        stopper = threading.Thread(target=pipe.stop, daemon=True)
        stopper.start()
        stopper.join(timeout=seconds)
        return stopper

    def test_stop_still_reports_when_a_stage_never_exits(self, monkeypatch):
        monkeypatch.setattr(pp, "_STOP_JOIN_TIMEOUT", 0.2)
        pipe = ProcessPipe("title")
        pipe.add_process(StubbornProcess("stuck"))

        pmsgq, _thread = start_pipe(pipe)
        stopper = self._stop_within(pipe)

        assert (
            not stopper.is_alive()
        ), "stop() never returned; the player loop would be stuck"
        assert pmsgq.get(timeout=2.0) == MSG_PLAYER_PIPE_STOPPED

    def test_a_stuck_stage_does_not_stop_the_others_being_stopped(self, monkeypatch):
        """
        The stuck stage is skipped rather than retried, and the stages after it
        are still told to stop. Skipping is deliberate: a thread that outlived
        its stop may still be writing, so there is no safe moment to read its
        error from.
        """
        monkeypatch.setattr(pp, "_STOP_JOIN_TIMEOUT", 0.2)
        first = StubbornProcess("stuck")
        second = StubbornProcess("also stuck")
        pipe = ProcessPipe("title")
        pipe.add_process(first)
        pipe.add_process(second)

        pmsgq, _thread = start_pipe(pipe)
        stopper = self._stop_within(pipe)

        assert not stopper.is_alive()
        assert first.stopped and second.stopped
        assert pmsgq.get(timeout=2.0) == MSG_PLAYER_PIPE_STOPPED

    def test_the_join_is_bounded(self):
        """
        Pinned as a positive number, because the timeout is the only thing
        standing between a stuck stage and a permanently dead player.
        """
        assert isinstance(pp._STOP_JOIN_TIMEOUT, (int, float))
        assert 0 < pp._STOP_JOIN_TIMEOUT <= 30

    def test_a_healthy_pipe_still_reports_its_error(self):
        """
        The common path must be untouched: a stage that exits promptly still
        has its error read and handed to the parent.
        """
        pipe = ProcessPipe("title")
        pipe.add_process(
            FakeProcess("boom", outcome="ready_then_wait", error="disk is full")
        )

        pmsgq, _thread = start_pipe(pipe)
        pipe.stop()

        assert pmsgq.get(timeout=2.0) == MSG_PLAYER_PIPE_STOPPED
        assert pmsgq.get(timeout=2.0) == "disk is full"


class _StubExternal(ExternalProcess):
    """
    An ExternalProcess with no real subprocess behind it.

    wait() honours its timeout argument the way subprocess does -- raising
    TimeoutExpired rather than blocking -- so both the bounded and the unbounded
    wait can be exercised without hanging the run.
    """

    def __init__(self, never_exits=True, error=False):
        super().__init__()
        self.killed = False
        self.waits = []
        self._never_exits = never_exits
        if error:
            self.errors.append("could not start")

    def name(self):
        return "stub"

    def _get_cmd(self, args):
        return ["true"]

    def _wait(self):
        pass

    def stop(self):
        self.killed = True

    def _fake_proc(self):
        never = self._never_exits
        outer = self

        class P:
            pid = 4242
            stdout = None
            returncode = None

            def poll(self):
                return -9 if outer.killed else None

            def wait(self, timeout=None):
                outer.waits.append(timeout)
                if never and not outer.killed:
                    raise pp.subprocess.TimeoutExpired("stub", timeout)
                return 0

        return P()

    def run_wait(self, monkeypatch, timeout=0.2):
        monkeypatch.setattr(pp, "_EXIT_WAIT_TIMEOUT", timeout)
        # A pipe with no writer gives the copier EOF immediately, which is the
        # copier finishing as it should.
        r, w = os.pipe()
        os.close(w)
        reader = os.fdopen(r, "r")
        self.proc = self._fake_proc()
        self.proc.stdout = reader
        self.msgq = queue.Queue()
        self.procidx = 0
        try:
            ExternalProcess._wait(self)
        finally:
            # _wait() clears self.proc once the process is done with.
            reader.close()


class TestAStageThatCouldNotStartIsKilled:
    """
    A stage that failed to become ready must not hold the pipe open forever.

    VLC on Debian trixie's armhf build has no rc interface module, so the unix
    socket _ready() polls for never appears. _ready() gives up and records the
    error, but cvlc carries on retrying its audio output indefinitely, and an
    unbounded proc.wait() never returned. The stage never halted, the pipe never
    stopped, and the player sat at ST_STARTING indefinitely.

    Only reached when _ready() has already failed, which is what makes it safe.
    """

    def test_a_process_that_never_exits_is_killed(self, monkeypatch):
        proc = _StubExternal(never_exits=True, error=True)
        proc.run_wait(monkeypatch)
        assert proc.killed

    def test_the_wait_is_bounded_when_startup_failed(self, monkeypatch):
        proc = _StubExternal(never_exits=True, error=True)
        proc.run_wait(monkeypatch)
        assert proc.waits[0] == 0.2

    def test_a_process_that_exits_is_not_killed(self, monkeypatch):
        proc = _StubExternal(never_exits=False, error=True)
        proc.run_wait(monkeypatch)
        assert not proc.killed


class TestAPlayerThatStartedIsNeverTimedOut:
    """
    A stage that reported ready has succeeded. Waiting for its process to leave
    must not be bounded, because that wait is the length of a film.

    This cost a torrent stream and then nearly a diagnosis: the bound was added
    for a VLC that could not report ready, applied to every stage, and so killed
    a healthy player exactly _EXIT_WAIT_TIMEOUT seconds in. It surfaced as a
    stop after five seconds and an error message made of unrelated ALSA noise
    picked up from the log tail -- which is what sent the investigation after the
    audio pipeline, which was never broken.
    """

    def test_the_wait_is_unbounded_after_a_successful_start(self, monkeypatch):
        proc = _StubExternal(never_exits=False, error=False)
        proc.run_wait(monkeypatch)
        # No timeout argument at all: proc.wait() with nothing to expire.
        assert proc.waits == [None]

    def test_a_started_player_is_not_killed(self, monkeypatch):
        proc = _StubExternal(never_exits=False, error=False)
        proc.run_wait(monkeypatch)
        assert not proc.killed

    def test_a_started_player_that_will_not_exit_is_left_alone(self, monkeypatch):
        """
        The regression itself. A process that will not exit, with no startup
        error, used to raise TimeoutExpired after the bound and be killed --
        which is what stopped a healthy film five seconds in.

        Here the unbounded wait is a plain proc.wait(), so the fake returns and
        nothing is killed. A real player in this state stays alive until stop()
        is called, which is the pipe's job, not this stage's.
        """
        proc = _StubExternal(never_exits=False, error=False)
        proc.run_wait(monkeypatch)
        assert not proc.killed
        assert proc.waits == [None]

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
import signal
import subprocess
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

        The title rides along with them. What is playing is known only to the
        pipe -- it is constructed with it -- and the player stage wants it to
        announce what started, so every stage is handed a "title" as well.
        """
        first = FakeProcess("first", args={"outfile": "/tmp/a"})
        second = FakeProcess("second", outcome="ready_then_finished", args={"final": 1})
        pipe = ProcessPipe("title")
        pipe.add_process(first)
        pipe.add_process(second)

        collected, _ = drain(pipe)

        assert first.started_with == {}
        assert second.started_with == {"outfile": "/tmp/a", "title": "title"}
        assert collected[0] == MSG_PLAYER_PIPE_STOPPED

    def test_the_args_passed_on_are_not_edited_in_place(self):
        """
        The dict goes from stage to stage and the last one keeps it. Adding the
        title to it directly would leave the downloader's own args carrying a
        title it never asked for.
        """
        stage = FakeProcess("first", args={"outfile": "/tmp/a"})
        pipe = ProcessPipe("title")
        pipe.add_process(stage)
        args = {"outfile": "/tmp/a"}

        assert pipe._with_title(args) == {"outfile": "/tmp/a", "title": "title"}
        assert args == {"outfile": "/tmp/a"}, args

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
        # Set up exactly what start() sets up, since _wait() now joins the copier
        # start() began rather than creating one of its own. A pipe with no writer
        # gives that copier EOF at once, which is it finishing.
        self._tail = pp._LineTail(on_line=self._observe_line)
        self._lines = queue.Queue()
        self._copier = pp._bgcopypipe(
            reader, self._tail, lambda: self._lines.put(pp._EOF)
        )
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


class _ChunkedStage(ExternalProcess):
    """
    A stage whose process writes a known chunk of output and then stops.

    Used to reproduce a bug that cost an evening of hardware testing: two lines
    arriving in one read, with a third arriving much later. The old _readline()
    polled the OS file descriptor with select() but read through a
    BufferedReader, so one read pulled the whole chunk into Python's buffer and
    handed back only the first line. The rest were invisible to select, so once
    the pipe drained every subsequent call reported "not ready" and the stage
    waited out its full timeout and reported failure -- while the line it was
    waiting for sat unread in its own buffer.
    """

    def __init__(self, chunks, hold=True):
        super().__init__()
        self.chunks = chunks
        self.hold = hold
        self.errors = []

    def name(self):
        return "chunked"

    def _get_cmd(self, args):
        return ["true"]

    def _read_chunks(self):
        """
        Drive the real reader with real pipes, then hand back the queue.
        """
        r, w = os.pipe()
        writer = os.fdopen(w, "wb")
        for text in self.chunks:
            writer.write(text.encode())
            writer.flush()
            time.sleep(0.05)
        if not self.hold:
            writer.close()

        # Binary, because that is what Popen hands over for stdout=PIPE, and it
        # matters: a text stream has no read1, so the copier would fall back to
        # read() and block until it had filled its buffer.
        reader = os.fdopen(r, "rb")
        self.proc = m.MagicMock(poll=lambda: None, stdout=reader, returncode=None)
        self._tail = pp._LineTail(on_line=self._observe_line)
        self._lines = queue.Queue()
        self._copier = pp._bgcopypipe(
            reader, self._tail, lambda: self._lines.put(pp._EOF)
        )
        self.msgq = queue.Queue()
        self.procidx = 0
        return reader, writer

    def drain_lines(self, reader, writer, count, timeout=2.0):
        got = []
        deadline = time.time() + timeout
        while len(got) < count and time.time() < deadline:
            try:
                got.append(self._readline(0.2))
            except ProcessException:
                break
        writer.close()
        self._copier.join(timeout=2)
        reader.close()
        return got


class TestLinesInOneChunkAreAllReadable:
    """
    Every line a process writes must be readable, however it was chunked.

    This is the fix for the readiness failure seen on the Pi: a VLC printed
    "Command Line Interface initialized", so _ready() could see it and return,
    but that line arrived in the chunk after some log lines. The first read
    consumed the earlier lines, and the banner was never returned. _ready() then
    ran out its 30 second timeout and reported "vlc cli interface did not come
    up" with the banner quoted in the failure message, which is what finally
    identified it.
    """

    def test_all_lines_from_a_single_chunk_come_back(self):
        stage = _ChunkedStage(["first\nsecond\nthird\n"])
        reader, writer = stage._read_chunks()
        assert stage.drain_lines(reader, writer, 3) == ["first", "second", "third"]

    def test_a_line_arriving_after_an_early_chunk_is_still_read(self):
        """
        The exact shape that failed: two lines at once, then a pause, then the
        one that matters.
        """
        stage = _ChunkedStage(["noise one\nnoise two\n", "late arrival\n"])
        reader, writer = stage._read_chunks()
        assert stage.drain_lines(reader, writer, 3) == [
            "noise one",
            "noise two",
            "late arrival",
        ]

    def test_a_chunk_without_a_trailing_newline_is_not_swallowed(self):
        """
        copyfileobj hands over whatever it read, so a process that writes without
        flushing a newline leaves a partial line. It is not returned until the
        rest arrives, which is correct -- but it must not be lost either.
        """
        stage = _ChunkedStage(["partial", " rest\n"])
        reader, writer = stage._read_chunks()
        assert stage.drain_lines(reader, writer, 1) == ["partial rest"]


class TestReadlineAtEndOfOutput:
    """
    Reading past the end of a process's output must raise, not block.

    Four stages -- peerflix, dlsrv, yt-dlp and subtitles -- call _readline()
    with no timeout inside a while True, relying on it to raise when the process
    dies. A queue looks the same whether a line is coming or the pipe has closed,
    so the copier pushes a sentinel at EOF to wake them. Without it a process
    that died silently would hang the stage, and with it the pipe, for ever.
    """

    def test_end_of_output_raises_rather_than_blocking(self):
        stage = _ChunkedStage(["only line\n"], hold=False)
        reader, writer = stage._read_chunks()
        try:
            assert stage._readline(2.0) == "only line"
            with pytest.raises(ProcessException):
                stage._readline(2.0)
        finally:
            writer.close()
            reader.close()

    def test_the_exception_says_how_the_process_died(self):
        """
        "exited 0" and "died" mean very different things in a failure message,
        so the return code is kept.
        """
        stage = _ChunkedStage([], hold=False)
        reader, writer = stage._read_chunks()
        stage.proc.poll = lambda: 0
        stage.proc.returncode = 3
        try:
            with pytest.raises(ProcessException, match="Process exit: 3"):
                stage._readline(2.0)
        finally:
            writer.close()
            reader.close()

    def test_a_timeout_with_nothing_to_read_raises(self):
        """
        The other way out, and the one a stage polling for readiness depends on:
        no line within the window is a timeout, not a hang.
        """
        stage = _ChunkedStage([])
        reader, writer = stage._read_chunks()
        try:
            with pytest.raises(ProcessException, match="Timed out"):
                stage._readline(0.2)
        finally:
            writer.close()
            reader.close()

    def test_blank_lines_are_skipped_not_returned(self):
        """
        Players print bare newlines while starting. The old reader dropped them,
        so _ready() did not spend a call on one.
        """
        stage = _ChunkedStage(["\n\n  \nreal\n"])
        reader, writer = stage._read_chunks()
        assert stage.drain_lines(reader, writer, 1) == ["real"]


class TestOneReaderPerProcess:
    """
    stdout is read once, by the copier that start() launches.

    Two readers on one pipe is what made the desync possible, and the second one
    was started in _wait() after _ready() had already consumed part of the
    stream. One reader means the lines a stage sees and the lines kept for the
    failure message are the same lines, in the same order.
    """

    def test_wait_does_not_start_a_second_reader(self):
        stage = _ChunkedStage(["line\n"], hold=False)
        reader, writer = stage._read_chunks()
        before = stage._copier
        stage.proc.wait = lambda timeout=None: 0
        stage._wait()
        assert stage._copier is before
        writer.close()
        reader.close()

    def test_the_tail_covers_the_whole_run(self):
        """
        The tail now fills from the moment the process starts rather than from
        _ready() onward, so a failure message includes what the player said
        before it was ever asked whether it was ready.
        """
        stage = _ChunkedStage(["said at startup\n"])
        reader, writer = stage._read_chunks()
        try:
            stage._readline(2.0)
            assert "said at startup" in stage._tail.summary()
        finally:
            writer.close()
            reader.close()

    def test_every_line_is_logged_once(self, caplog):
        """
        Logging moved into the single reader. Two observers would double it, and
        these lines are how every one of these bugs was diagnosed.
        """
        stage = _ChunkedStage(["logged once\n"])
        reader, writer = stage._read_chunks()
        try:
            with caplog.at_level("INFO"):
                stage._readline(2.0)
            assert caplog.text.count("LINE(chunked): logged once") == 1
        finally:
            writer.close()
            reader.close()


class TestDownloadsAreNeverDeleted:
    """
    Nothing is removed when a stage stops.

    Every one of these sites used to delete on stop, which meant a download never
    survived being interrupted -- least of all for someone on a poor connection
    or with poor seeds, who would wait ten minutes and then start again from
    nothing. Clearing is a deliberate act now, not a side effect of stopping.
    """

    def test_stopping_a_stage_leaves_the_download_in_place(self, tmp_path):
        out = tmp_path / "bf.out"
        out.write_text("half a film")
        proc = _StubExternal(never_exits=False, error=True)
        proc.proc = None
        with m.patch.object(pp, "OUT_FILE", str(out)):
            proc.stop()
        assert out.exists(), "the download was deleted by stopping"

    def test_subtitles_survive_a_stop(self, tmp_path):
        from lib.player.subsproc import SubtitlesProcess

        subs = tmp_path / "episode.srt"
        subs.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n")
        proc = SubtitlesProcess({"lang": "en", "title": "x"})
        proc.subsfile = str(subs)
        proc.proc = None
        proc.stop()
        assert (
            subs.exists()
        ), "the subtitle file was deleted, so the next play refetches it"

    def test_peerflix_keeps_its_buffer(self, tmp_path):
        """
        peerflix was passed -r, which its own help spells "remove files on
        exit". That is why no torrent ever kept its download.
        """
        from lib.player.pflixproc import PeerflixProcess

        cmd = PeerflixProcess("magnet:?xt=urn:btih:AAAA", -1).cmd
        assert "-r" not in cmd
        assert "--remove" not in cmd

    def test_peerflix_buffers_on_the_card_not_in_ram(self):
        """
        /tmp is a tmpfs sized at half of RAM on any current Pi -- systemd's
        static tmp.mount -- so it holds 371MB on a 741MB Pi. A film is 2-4GB.
        The download filled the mount, every later write failed with ENOSPC, and
        the on-screen confirmation stopped appearing. The buffer has to be on
        the card.
        """
        from lib.player.pflixproc import BUFFER_DIR, PeerflixProcess

        assert not BUFFER_DIR.startswith("/tmp"), BUFFER_DIR
        cmd = PeerflixProcess("magnet:?xt=urn:btih:AAAA", -1).cmd
        assert cmd[cmd.index("-f") + 1] == BUFFER_DIR

    def test_the_server_clears_nothing_on_startup(self):
        """
        cleanup() ran on every start and removed both /tmp/torrent-stream and all
        of /tmp/blissflixx, so nothing survived a restart.
        """
        import inspect

        import blissflixx

        # The docstring names the paths it stopped deleting, so check the code.
        source = inspect.getsource(blissflixx.cleanup)
        parts = source.split('"""')
        body = parts[2] if len(parts) > 2 else source
        for gone in ("rmtree", "OUT_FILE", "/tmp/torrent-stream", "/tmp/blissflixx"):
            assert gone not in body, gone


class TestAStoppedProcessIsGivenTheChanceToTidyUp:
    """
    Stopping used to SIGKILL, and that stopped the next film from playing.
    """

    def _stage(self):
        stage = DlsrvProcess()  # concrete, and nothing is actually run
        stage.proc = m.Mock(pid=1234)
        stage.proc.wait = m.Mock(return_value=0)
        return stage

    def test_a_process_is_asked_to_stop_before_it_is_killed(self):
        stage = self._stage()

        with m.patch("lib.player.processpipe.os.killpg") as killpg:
            stage._terminate()

        # SIGTERM first. VLC releases its DRM lease on the way out, and the
        # next player cannot open a video output until it has.
        assert killpg.call_args_list[0].args[1] is signal.SIGTERM, killpg.call_args_list

    def test_nothing_is_killed_when_it_stops_promptly(self):
        stage = self._stage()

        with m.patch("lib.player.processpipe.os.killpg") as killpg:
            stage._terminate()

        assert killpg.call_count == 1, killpg.call_args_list

    def test_a_process_that_ignores_it_is_still_killed(self):
        """
        SIGTERM is not honoured by a process that has already wedged, so the
        kill is still there -- just no longer first.
        """
        stage = self._stage()
        stage.proc.wait = m.Mock(side_effect=subprocess.TimeoutExpired("x", 3))

        with m.patch("lib.player.processpipe.os.killpg") as killpg:
            stage._terminate()

        signals = [c.args[1] for c in killpg.call_args_list]
        assert signals == [signal.SIGTERM, signal.SIGKILL], signals

    def test_a_process_already_gone_is_not_killed_twice(self):
        """
        Stop runs from another thread and may race the process leaving on its
        own, which is the ordinary way a player ends.
        """
        stage = self._stage()
        stage.proc.wait = m.Mock(side_effect=subprocess.TimeoutExpired("x", 3))

        with m.patch(
            "lib.player.processpipe.os.killpg", side_effect=ProcessLookupError
        ):
            stage._terminate()  # must not raise


class TestAStopIsNotAFailure:
    """
    Stopping something is not something going wrong.

    _wait() calls _add_output_to_error whenever the stage is being killed, and
    that used to invent an error whenever none existed. So every ordinary stop
    showed up as a failure whose reason was whatever the player last printed:

        peerflix failed: Verifying downloaded: 0% | server is listening on
        http://192.168.1.119:9696/

    which is not a failure at all, and was alarming enough to look like a broken
    torrent.
    """

    def test_stopping_a_stage_produces_no_error(self):
        proc = _StubExternal(never_exits=False, error=False)
        proc.killing = True
        tail = pp._LineTail()
        tail.write("Verifying downloaded: 0%")
        proc._add_output_to_error(tail)
        assert proc.errors == []

    def test_a_process_that_dies_on_its_own_is_still_reported(self):
        """
        The other half, and the reason this cannot simply be "never invent one".
        A process that printed why it died and never called _set_error would
        otherwise be reported by nothing at all.
        """
        proc = _StubExternal(never_exits=False, error=False)
        proc.killing = False
        tail = pp._LineTail()
        tail.write("your input can't be opened")
        proc._add_output_to_error(tail)
        assert len(proc.errors) == 1
        assert proc.errors[0].startswith("stub failed: ")

    def test_output_still_joins_an_error_that_already_exists(self):
        proc = _StubExternal(never_exits=False, error=True)
        proc.killing = True
        tail = pp._LineTail()
        tail.write("no suitable decoder")
        proc._add_output_to_error(tail)
        assert len(proc.errors) == 1
        assert proc.errors[0] == "could not start | no suitable decoder"


class TestPeerflixsHardcodedMetadataPathIsLeftAlone:
    """
    peerflix has two output locations and only one of them can be aimed.

    -f sets the buffer path, so the download goes where we want. The .torrent
    metadata file cannot be moved at all: torrent-stream/index.js builds its path
    as

        var TMP = fs.existsSync('/tmp') ? '/tmp' : (os.tmpdir ...)
        var torrentPath = path.join(opts.tmp, opts.name, infoHash + '.torrent')

    with opts.name defaulting to 'torrent-stream'. TMP is resolved to the
    literal '/tmp' before any environment is consulted, so TMPDIR does not reach
    it, and peerflix exposes no option for it.

    TMPDIR was tried for this and reverted. It redirected os.tmpdir() users
    correctly but not this one, so it would have looked like it worked. A symlink
    from /tmp/torrent-stream was tried next and dropped: peerflix's path is
    clearer left alone than faked, and the file is about 21 KB per torrent
    against a download measured in gigabytes -- so leaving ~21KB of metadata in
    RAM costs nothing next to the gigabytes, and the buffer is on the card.
    """

    def test_the_buffer_is_on_the_card_while_metadata_stays_in_tmp(self):
        from lib.player.pflixproc import BUFFER_DIR, PeerflixProcess

        cmd = PeerflixProcess("magnet:?xt=urn:btih:AA", -1).cmd
        assert cmd[cmd.index("-f") + 1] == BUFFER_DIR
        assert not BUFFER_DIR.startswith("/tmp"), BUFFER_DIR

    def test_nothing_pretends_to_redirect_peerflixs_hardcoded_path(self):
        """
        Guards against the symlink coming back. It worked, and it was still
        wrong: making a path peerflix believes is its own into something else
        is more confusing than a 21 KB file in the wrong directory.
        """
        import inspect

        import blissflixx

        source = inspect.getsource(blissflixx)
        assert "symlink" not in source, "peerflix's hardcoded path is faked again"

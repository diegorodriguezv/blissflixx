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
import queue
import threading
import time

import pytest

from lib.player.dlsrvproc import DlsrvProcess
from lib.player.omxproc import OmxplayerProcess
from lib.player.omxproc2 import OmxplayerProcess2
from lib.player.pflixproc import PeerflixProcess
from lib.player.processpipe import (
    MSG_PLAYER_PIPE_STOPPED,
    ExternalProcess,
    Process,
    ProcessException,
    ProcessPipe,
)
from lib.player.subsproc import SubtitlesProcess
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


class TestPipeThreadSafety:
    def test_two_pipes_are_independent(self):
        """
        Each pipe owns its own msgq and thread list, so running two at once
        must not cross-talk. This is the shape _Player uses when a new item is
        queued while the current one is still stopping.
        """
        p1 = ProcessPipe("one")
        p1.add_process(FakeProcess("a1", outcome="finished"))
        p2 = ProcessPipe("two")
        p2.add_process(FakeProcess("a2", outcome="finished"))

        c1, t1 = drain(p1, timeout=2.0)
        c2, t2 = drain(p2, timeout=2.0)

        assert c1[0] == MSG_PLAYER_PIPE_STOPPED
        assert c2[0] == MSG_PLAYER_PIPE_STOPPED
        assert p1.msgq is not p2.msgq


class TestPipeStatusMsgIndexing:
    def test_status_msg_with_no_processes_added(self):
        """
        An empty pipe has no process to ask for a status message, and
        status_msg() indexes procs[0], so it raises. Unreachable in practice:
        _Player.play() always adds at least one process before the pipe is used.
        Pinned so that making the empty case work is a deliberate change.
        """
        pipe = ProcessPipe("empty")
        with pytest.raises(IndexError):
            pipe.status_msg()


class TestProcessContract:
    """
    Process and ExternalProcess are abstract bases now. These cover the
    contract itself, which the rest of this file relies on.
    """

    def test_process_cannot_be_instantiated(self):
        with pytest.raises(TypeError):
            Process()

    def test_incomplete_subclass_cannot_be_instantiated(self):
        """
        Previously a subclass missing name()/start()/stop() could still be
        constructed and only failed later, at playback time, with a bare
        NotImplementedError from deep inside a thread.
        """

        class Incomplete(Process):
            def name(self):
                return "incomplete"

        with pytest.raises(TypeError):
            Incomplete()

    def test_external_process_requires_get_cmd(self):
        class NoCmd(ExternalProcess):
            def name(self):
                return "nocmd"

            def start(self, args):
                pass

            def stop(self):
                pass

        with pytest.raises(TypeError):
            NoCmd()

    def test_get_cmd_signature_takes_args(self):
        """
        The base used to declare _get_cmd(self) while every implementation and
        the call site in start() used _get_cmd(args). The arity mismatch was
        invisible because the declaration was never reached.
        """
        for cls in (
            DlsrvProcess,
            OmxplayerProcess,
            OmxplayerProcess2,
            PeerflixProcess,
            SubtitlesProcess,
            YoutubeDlProcess,
        ):
            params = list(inspect.signature(cls._get_cmd).parameters)
            assert params == ["self", "args"], cls.__name__

    def test_shell_flag_is_plumbed_through(self):
        """
        shell=True is used by the two omxplayer stages, since their commands
        are shell strings with pipes and redirects in them.
        """
        assert OmxplayerProcess().shell is True
        assert OmxplayerProcess2().shell is True

    def test_base_process_control_is_a_no_op(self):
        """
        Stages that cannot act on a control action ignore it, rather than
        raising. Player.control("pause") reaching a downloader used to raise
        AttributeError and surface as a 500 in the UI.
        """
        proc = FakeProcess("x")
        assert proc.control("pause") is None

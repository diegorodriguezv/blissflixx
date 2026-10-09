import os
import select
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from queue import Queue
from threading import Thread

import cherrypy

MSG_PROCESS_READY = 1
MSG_PROCESS_HALTED = 2
MSG_PROCESS_FINISHED = 3

MSG_PLAYER_PIPE_STOPPED = 4

TMP_DIR = "/tmp/blissflixx"
OUT_FILE = "/tmp/blissflixx/bf.out"
#: Seconds to wait for a finished process's output to be read out of its pipe
#: before giving up on it.
_COPY_DRAIN_TIMEOUT = 5
#: Seconds to wait for a stage's thread to exit after being told to stop.
#:
#: This join used to have no timeout, which made stop() capable of never
#: returning. A stage that will not exit -- a yt-dlp blocked in a network read
#: is the one that actually turned this up on a Pi being rate-limited -- meant
#: the join never completed, so the MSG_PLAYER_PIPE_STOPPED below was never
#: emitted and the player loop waited forever for a message that could not
#: arrive. Every later play was then dropped without a trace. Bounding the join
#: makes the stop notification unconditional, which is the whole property the
#: loop depends on to make progress.
_STOP_JOIN_TIMEOUT = 5
#: Seconds to wait for a spawned process to exit after its stage has finished
#: with it. Only reached when _ready() has already failed, so the process has
#: already had its chance to exit on its own.
_EXIT_WAIT_TIMEOUT = 5


def _start_thread(target, *args):
    th = Thread(target=target, args=args)
    th.daemon = True
    th.start()
    return th


class _DiscardFile:
    def write(self, *args):
        pass

    def close(self):
        pass


class _LineTail:
    """
    A file-like sink that keeps the last few lines written to it.

    Used as the destination for a process's output, so that when a stage fails
    its explanation survives. Players report why they cannot start on stdout or
    stderr and nowhere else, and discarding it left the pipe with nothing to say
    beyond "it stopped".

    Keeps lines rather than raw chunks because players are chatty and the
    interesting line is rarely the last byte written.
    """

    def __init__(self, keep=12, on_line=None):
        self.keep = keep
        self.lines = []
        self._partial = ""
        # Optional per-line observer. A stage that is controlled by typing
        # commands at the process reads its replies from the same stream the
        # process logs to, so it needs the lines rather than just the tail.
        self.on_line = on_line

    def write(self, text):
        """
        Accepts bytes or str.

        A subprocess pipe is opened in binary mode, so shutil.copyfileobj writes
        bytes to this. The old sink took whatever it was given and ignored it,
        which is why the mismatch went unnoticed: the TypeError was swallowed
        by _copypipe's except clause and the output simply vanished.
        """
        if isinstance(text, (bytes, bytearray)):
            text = text.decode("utf-8", "replace")
        if not text:
            return
        self._partial += text
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            self._push(line)
        # Bound the partial buffer too, in case a stage writes one huge line with
        # no newline: keep its tail, which is where the message ends.
        if len(self._partial) > 2000:
            self._partial = self._partial[-2000:]

    def _push(self, line):
        line = line.strip()
        if not line:
            return
        self.lines.append(line)
        if len(self.lines) > self.keep:
            del self.lines[: len(self.lines) - self.keep]
        if self.on_line is not None:
            try:
                self.on_line(line)
            except Exception:
                # An observer that raises must not take the copier down with
                # it and lose the output it was observing.
                pass

    def close(self):
        if self._partial.strip():
            self._push(self._partial)
        self._partial = ""

    def __len__(self):
        return len(self.lines)

    def __bool__(self):
        # A process that died mid-line has still said something, and that
        # fragment is often the reason. Only complete lines are in self.lines
        # until close(), so _partial has to count.
        return bool(self.lines) or bool(self._partial.strip())

    def summary(self, max_lines=3):
        """The last few lines, which is where a failure reason usually is."""
        lines = list(self.lines)
        # A process that died mid-line left a fragment. Non-destructive, so this
        # can be called before close() without losing the pending line.
        partial = self._partial.strip()
        if partial and (not lines or lines[-1] != partial):
            lines.append(partial)
        if not lines:
            return "no output"
        return " | ".join(lines[-max_lines:])


def _copypipe(src, dest):
    if dest is None:
        dest = _DiscardFile()

    # Ignore broken pipe errors if process
    # are forced to stop
    try:
        shutil.copyfileobj(src, dest)
    except Exception:
        pass

    src.close()
    dest.close()


def _bgcopypipe(src, dest):
    return _start_thread(_copypipe, src, dest)


class ProcessException(Exception):
    pass


class ProcessPipe:
    def __init__(self, title):
        self.title = title
        self.procs = []
        self.threads = []
        self.msgq = Queue()
        self.next_proc = 0
        self.stopping = False
        self.started = False

    def status_msg(self):
        if self.started:
            return self.title
        else:
            idx = self.next_proc - 1
            if idx < 0:
                idx = 0
            return self.procs[idx].status_msg()

    def add_process(self, proc):
        self.procs.append(proc)

    def start(self, pmsgq):
        self.pmsgq = pmsgq
        self._start_next()
        while True:
            m = self.msgq.get()
            idx = self.msgq.get()
            name = self.procs[idx].name()

            if m == MSG_PROCESS_READY:
                cherrypy.log("READY: " + name)
                args = self.msgq.get()
                if not self._is_last_proc(idx):
                    self._start_next(args)
                else:
                    self.started = True

            elif m == MSG_PROCESS_FINISHED:
                cherrypy.log("FINISHED: " + name)
                if self._is_last_proc(idx):
                    self.stop()
                    break

            elif m == MSG_PROCESS_HALTED:
                cherrypy.log("HALTED: " + name)
                self.stop()
                break

    def _last_proc(self):
        return self.procs[len(self.procs) - 1]

    def _is_last_proc(self, idx):
        return idx == len(self.procs) - 1

    def _start_next(self, args={}):
        proc = self.procs[self.next_proc]
        cherrypy.log("STARTING: " + proc.name() + ' "' + repr(proc) + '"')
        proc.set_msgq(self.msgq, self.next_proc)
        self.threads.append(_start_thread(proc.start, args))
        self.next_proc = self.next_proc + 1

    def stop(self):
        if self.stopping:
            return
        self.stopping = True
        self.started = False
        error = None
        for idx in range(self.next_proc - 1, -1, -1):
            proc = self.procs[idx]
            proc.stop()
            self.threads[idx].join(timeout=_STOP_JOIN_TIMEOUT)
            if self.threads[idx].is_alive():
                # Skipped rather than joined again, and its error is not read:
                # a thread that outlived its stop may still be writing to the
                # stage, so there is no safe moment at which to read it.
                cherrypy.log("STAGE DID NOT EXIT: " + proc.name())
                continue
            if proc.has_error():
                error = proc.get_errors()[0]
                cherrypy.log("GOT ERROR: " + error)
        self.pmsgq.put(MSG_PLAYER_PIPE_STOPPED)
        self.pmsgq.put(error)

    def is_started(self):
        return self.started

    def is_stopping(self):
        return self.stopping

    def control(self, action):
        if self.is_started():
            self._last_proc().control(action)


class Process(ABC):
    """
    Base for one stage of a ProcessPipe.

    Subclasses must implement name(), start() and stop(); the msg_* methods
    implement the pipe protocol and are provided here.
    """

    def __init__(self):
        self.errors = []

    def set_msgq(self, msgq, procidx):
        self.msgq = msgq
        self.procidx = procidx

    def _send(self, msg, args=None):
        self.msgq.put(msg)
        self.msgq.put(self.procidx)
        if args is not None:
            self.msgq.put(args)

    def _set_error(self, msg):
        self.errors.append(msg)

    def get_errors(self):
        return self.errors

    def has_error(self):
        return len(self.errors) > 0

    def status_msg(self):
        return "LOADING STREAM"

    @abstractmethod
    def name(self):
        """Short label used in logs and status messages."""

    @abstractmethod
    def start(self, args):
        """Run the stage. Call msg_ready/msg_finished/msg_halted to report back."""

    @abstractmethod
    def stop(self):
        """Tear the stage down. Called from another thread, so must be safe
        to race against start()."""

    def control(self, action):
        """
        Handle a playback control action. Only stages that can act on one need
        to override this; the default is to ignore it.
        """

    def msg_ready(self, args=None):
        if args is None:
            args = {}
        self._send(MSG_PROCESS_READY, args)

    def msg_halted(self):
        self._send(MSG_PROCESS_HALTED)

    def msg_finished(self):
        self._send(MSG_PROCESS_FINISHED)


class ExternalProcess(Process):
    """
    A pipeline stage backed by an external command.

    Subclasses supply _get_cmd() and usually _ready(); start() here does the
    work of spawning the command, waiting for it to report readiness, then
    draining its output until it exits.
    """

    def __init__(self, shell=False, stdin_pipe=False):
        super().__init__()
        self.shell = shell
        # A stage driven by typing commands into its own process needs a pipe to
        # write them to. Off by default so every other stage keeps inheriting
        # stdin exactly as before; VLC is currently the only one that asks.
        self.stdin_pipe = stdin_pipe
        # Set by a stage that needs each output line as it arrives. See
        # _LineTail.on_line.
        self.on_output_line = None
        self.killing = False
        if not os.path.exists(TMP_DIR):
            os.makedirs(TMP_DIR)

    def start(self, args):
        cmd = self._get_cmd(args)
        if isinstance(cmd, str):
            cherrypy.log("RUN: " + cmd)
        else:
            cherrypy.log("RUN: " + " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stderr=subprocess.STDOUT,
            stdout=subprocess.PIPE,
            # None means inherit, which is what every stage did before. The pipe
            # is never closed by us: closing it gives the process EOF on stdin,
            # which is exactly how the VLC cli interface used to exit the moment
            # it started.
            stdin=subprocess.PIPE if self.stdin_pipe else None,
            preexec_fn=os.setsid,
            shell=self.shell,
        )
        try:
            args = self._ready()
            self.msg_ready(args)
        except ProcessException as e:
            # Ignore errors if process is being killed
            if not self.killing:
                self._set_error(str(e))

        self._wait()

    def _add_output_to_error(self, tail):
        """
        Fold the process's own output into this stage's error.

        The pipe reports a stage's first error and nothing else, so the output
        has to go into that one rather than becoming a second entry that would
        never be read. Without this a player that printed why it could not start
        left the UI with a generic message and no way to act on it.
        """
        if not tail:
            return
        output = tail.summary()
        if self.errors:
            self.errors[0] = self.errors[0] + " | " + output
        else:
            self._set_error(self.name() + " failed: " + output)

    def _wait(self):
        # Drain stderr/stdout so the pipe cannot fill and block the process, but
        # keep the last few lines rather than discarding them. A player that
        # cannot start says why on stdout ("Failed to get xlease", for a DRM
        # lease it could not take), and discarding it left the pipe with nothing
        # to say beyond "it stopped".
        tail = _LineTail(on_line=self.on_output_line)
        copier = _bgcopypipe(self.proc.stdout, tail)
        # Bounded, because a process that cannot be waited for would otherwise
        # hold this stage open forever. A VLC that never reports ready -- no rc
        # module, so the socket never appears -- carries on retrying its audio
        # output indefinitely, and this wait is what never returned: the stage
        # never halted, the pipe never stopped, and the player sat at
        # ST_STARTING for good. Waiting is only reached once _ready() has already
        # failed, so the process has had its chance to finish and say why; the
        # timeout is the backstop, not the normal path. Killing it here rather
        # than at the failure keeps whatever it printed before it stalled.
        try:
            self.proc.wait(timeout=_EXIT_WAIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            cherrypy.log("PROCESS DID NOT EXIT: " + self.name())
            self.stop()
            self.proc.wait()
        self.proc = None
        # The copier finishes when stdout reaches EOF, which the exit just
        # caused. Joining it matters: reading tail before it has drained is a
        # race, and on the Pi that race reliably returned an empty tail, losing
        # the reason all over again. Timed, so a wedged reader cannot hold up
        # the pipe.
        copier.join(timeout=_COPY_DRAIN_TIMEOUT)

        if self.has_error() or self.killing:
            self._add_output_to_error(tail)
            self.msg_halted()
        else:
            self.msg_finished()

    def stop(self):
        if self.proc is not None:
            # Stop gets called from a seperate thread
            # so shutdown may already be in progress
            # when we try to kill - therefore ignore errors
            try:
                # kill - including all children of process
                self.killing = True
                os.killpg(self.proc.pid, signal.SIGKILL)
            except Exception:
                pass

        if os.path.exists(OUT_FILE):
            try:
                os.remove(OUT_FILE)
            except Exception:
                pass

    @abstractmethod
    def _get_cmd(self, args):
        """Return the command to run, as a string or argv list."""

    def _ready(self):
        """
        Block until the process has started successfully. Returning normally
        means ready; raising ProcessException means it failed.
        """

    def _readline(self, timeout=None):
        poll_obj = select.poll()
        poll_obj.register(self.proc.stdout, select.POLLIN)
        while self.proc.poll() is None:
            if timeout is not None:
                poll_result = poll_obj.poll(1000 * timeout)
                if not poll_result:
                    raise ProcessException("Timed out waiting for input")
            line = self.proc.stdout.readline()
            if not line:
                raise ProcessException("Process suddenly died")
            line = line.strip().decode("utf-8")
            cherrypy.log("LINE(" + self.name() + "): " + line)
            if line.strip() != "":
                return line
        raise ProcessException("Process exit: " + str(self.proc.returncode))

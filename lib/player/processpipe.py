import os
import shutil
import signal
import subprocess
from abc import ABC, abstractmethod
from queue import Empty, Queue
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


def _copypipe(src, dest, on_done=None):
    if dest is None:
        dest = _DiscardFile()

    # Read incrementally, one buffer at a time, and hand each one straight on.
    #
    # shutil.copyfileobj is not usable here. It calls src.read(length), and
    # read() on a BufferedReader blocks until it has that many bytes or hits EOF.
    # That was harmless when the copier only fed a log tail -- the lines could
    # wait -- but the copier now feeds readiness as well, and a player that
    # printed three lines and then sat there playing would never have them
    # delivered. read1() returns whatever has arrived instead of waiting for a
    # full buffer, which is what makes a line available the moment it is written.
    #
    # Broken pipe errors are ignored because a stage can be stopped underneath
    # this while it is still reading.
    try:
        read1 = getattr(src, "read1", None)
        while True:
            chunk = read1(65536) if read1 else src.read(65536)
            if not chunk:
                break
            dest.write(chunk)
    except Exception:
        pass

    src.close()
    dest.close()
    if on_done is not None:
        # Reached end of output. Whatever is blocked reading this pipe is about
        # to find out.
        on_done()


def _bgcopypipe(src, dest, on_done=None):
    return _start_thread(_copypipe, src, dest, on_done)


#: Pushed onto a stage's line queue when its process reaches end of output, so a
#: thread blocked in _readline() wakes up instead of waiting forever. Only a
#: sentinel can do that: the queue looks identical whether a line is coming or
#: the pipe has closed, and a process that dies silently would otherwise hang
#: every stage waiting on it.
_EOF = object()


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
        # Extra environment for the child. None inherits ours.
        self.env = None
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
            # None inherits this process's environment, which is what every
            # stage did before. A stage whose dependency writes somewhere of its
            # own choosing sets this to redirect it -- peerflix writes its
            # .torrent metadata to os.tmpdir() regardless of -f.
            env=self.env,
        )
        # One reader, started here, for the whole life of the process.
        #
        # This used to read stdout with select() during _ready() and then hand
        # the same handle to a copier for _wait(). That combination cannot work:
        # select watches the OS file descriptor, while readline() on a
        # BufferedReader pulls a whole 8 KB chunk into Python's buffer and
        # returns only the first line. The rest are stranded in that buffer where
        # select cannot see them, so once the OS pipe drained, select reported
        # "not ready" forever and every further read raised "Timed out".
        #
        # That is how a VLC printed its cli banner and still failed to start:
        # the banner arrived in the chunk after some log lines, so _ready()
        # consumed the first line and never saw it, spun for its full 30 second
        # timeout, and reported "vlc cli interface did not come up" with the
        # banner quoted in the failure message. Which stage it bites depends
        # entirely on how the output happens to be chunked, so it was a latent
        # bug in _readline for every backend, not something about VLC.
        #
        # Now a single copier owns stdout for the whole run. _ready() consumes
        # lines off a queue it fills, and _wait() joins that same copier instead
        # of starting a second reader. There is no select left to disagree with
        # the buffer.
        self._tail = _LineTail(on_line=self._observe_line)
        self._lines = Queue()
        self._copier = _bgcopypipe(
            self.proc.stdout, self._tail, lambda: self._lines.put(_EOF)
        )

        try:
            args = self._ready()
            self.msg_ready(args)
        except ProcessException as e:
            # Ignore errors if process is being killed
            if not self.killing:
                self._set_error(str(e))

        self._wait()

    def _observe_line(self, line):
        """
        Hand one complete line to everyone who wants it.

        Logging, the _readline queue and a stage's own on_output_line all happen
        here, once per line, because the copier is the only thing that reads the
        process's output. A stage's observer is called last so that a stage which
        consumes lines cannot lose one before it is logged.
        """
        cherrypy.log("LINE(" + self.name() + "): " + line)
        self._lines.put(line)
        if self.on_output_line is not None:
            self.on_output_line(line)

    def _add_output_to_error(self, tail):
        """
        Fold the process's own output into this stage's error.

        The pipe reports a stage's first error and nothing else, so the output
        has to go into that one rather than becoming a second entry that would
        never be read. Without this a player that printed why it could not start
        left the UI with a generic message and no way to act on it.

        Only ever appended to an error that already exists, or invented for a process
        that died on its own saying why. It is not invented for a deliberate
        stop, which is what _wait() also passes here.

        That distinction is the whole point of this method. It used to invent an
        error whenever there was none, so every ordinary stop became a failure.
        Stopping a torrent produced

            peerflix failed: Verifying downloaded: 0% | server is listening on
            http://192.168.1.119:9696/

        in the UI: not a failure at all, just the last thing peerflix said before
        it was asked to stop. But a process that died by itself, having printed
        the reason and never called _set_error, is a genuine failure and is still
        reported -- there is nothing else that will ever mention it.
        """
        if not tail:
            return
        output = tail.summary()
        if self.errors:
            self.errors[0] = self.errors[0] + " | " + output
        elif not self.killing:
            self._set_error(self.name() + " failed: " + output)

    def _wait(self):
        # The copier started back in start() and owns stdout for the whole run,
        # so there is nothing to drain here and nothing to discard. The tail it
        # fills keeps the last few lines, which is how a player that could not
        # start still says why ("Failed to get xlease", for a DRM lease it could
        # not take) instead of leaving the pipe with nothing to report but that
        # it stopped.
        # How long to wait for the process to leave is not one thing, it depends
        # on whether it ever arrived.
        #
        # If _ready() failed, the process is one that could not be started, and
        # waiting on it is bounded: a VLC with no rc module polls for a control
        # socket that can never appear, gives up after its timeout, and then
        # carries on retrying audio output indefinitely. Without a bound here
        # the stage never halted, the pipe never stopped, and the player sat at
        # ST_STARTING for good.
        #
        # If _ready() succeeded, the stage has done its job and the process is
        # now a player that will exit when the media ends. Bounding that wait
        # kills it mid-film: a VLC that reached cli readiness was SIGKILLed
        # exactly _EXIT_WAIT_TIMEOUT seconds in, which showed up as a torrent
        # that stopped after five seconds and an error message full of
        # unrelated ALSA noise from the log tail. There is no timeout here on
        # purpose. stop() is what ends a player that must not keep running, and
        # it already runs from the pipe.
        if self.has_error():
            try:
                self.proc.wait(timeout=_EXIT_WAIT_TIMEOUT)
            except subprocess.TimeoutExpired:
                cherrypy.log("PROCESS DID NOT EXIT: " + self.name())
                self.stop()
                self.proc.wait()
        else:
            self.proc.wait()
        self.proc = None
        # The copier finishes when stdout reaches EOF, which the exit just
        # caused. Joining it matters: reading the tail before it has drained is a
        # race, and on the Pi that race reliably returned an empty tail, losing
        # the reason all over again. Timed, so a wedged reader cannot hold up
        # the pipe.
        self._copier.join(timeout=_COPY_DRAIN_TIMEOUT)

        if self.has_error() or self.killing:
            self._add_output_to_error(self._tail)
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

        # The download is not deleted. It used to be, right here, by removing
        # OUT_FILE -- so a download that was interrupted, or stopped on purpose,
        # started again from nothing. Someone on a poor connection or with poor
        # peers would never finish anything. Clearing this out is a deliberate
        # user action now, never a side effect of stopping.

    @abstractmethod
    def _get_cmd(self, args):
        """Return the command to run, as a string or argv list."""

    def _ready(self):
        """
        Block until the process has started successfully. Returning normally
        means ready; raising ProcessException means it failed.
        """

    def _readline(self, timeout=None):
        """
        Take one line of the process's output.

        Reads from the queue the copier fills, so this never inspects the pipe
        itself and cannot disagree with the copier about what has been read.

        With no timeout this blocks until a line arrives or the process ends.
        With a timeout it gives up after that many seconds and raises.

        Both raise ProcessException at end of output, which is what the stages
        waiting on a startup line depend on to stop looping: peerflix, dlsrv and
        yt-dlp all call this with no timeout inside a while True, and would
        otherwise block forever on a process that died silently.
        """
        try:
            line = self._lines.get(timeout=timeout)
        except Empty:
            raise ProcessException("Timed out waiting for input")
        if line is _EOF:
            # The copier reached the end of stdout. Report it the way a blocking
            # readline() used to: as a death, and with the exit code when there
            # is one, because "exited 0" and "died" read very differently in a
            # failure message.
            if self.proc is not None and self.proc.poll() is not None:
                raise ProcessException("Process exit: " + str(self.proc.returncode))
            raise ProcessException("Process suddenly died")
        if not line.strip():
            # Blank lines are not output worth reporting. mpv in particular
            # prints a bare prompt before it is ready for anything.
            return self._readline(timeout)
        return line.strip()

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
#: How long a stage is given to stop tidying up before it is killed. Short,
#: because it is on the path to the next film starting: VLC releases its DRM
#: lease as it exits, and the next player cannot open a video output until it
#: has. See _terminate().
_TERMINATE_GRACE = 3
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

    def __init__(self, keep=12, on_line=None, partial_limit=2000):
        self.keep = keep
        self.lines = []
        self._partial = ""
        self.partial_limit = partial_limit
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
        # no newline: keep its tail, which is where the message ends. Stages that
        # need to parse a single long line -- yt-dlp's JSON is often half a
        # megabyte on one line -- raise this for themselves.
        if len(self._partial) > self.partial_limit:
            self._partial = self._partial[-self.partial_limit :]

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

    def summary(self, max_lines=3, line_limit=500):
        """The last few lines, which is where a failure reason usually is."""
        lines = list(self.lines)
        # A process that died mid-line left a fragment. Non-destructive, so this
        # can be called before close() without losing the pending line.
        partial = self._partial.strip()
        if partial and (not lines or lines[-1] != partial):
            lines.append(partial)
        if not lines:
            return "no output"
        # yt-dlp's single JSON line can be hundreds of kilobytes long. Handing
        # that whole thing to the UI turns a one-line failure into a wall of
        # JSON that hides the actual reason, so each line is clipped for the
        # summary. The full line is still kept in self.lines for whoever needs it.
        clipped = []
        for line in lines[-max_lines:]:
            if line_limit and len(line) > line_limit:
                line = line[: line_limit - 3] + "..."
            clipped.append(line)
        return " | ".join(clipped)


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
        #: Whether the last stage finished on its own, which is the film ending
        #: rather than being stopped or failing. Reported to the player with the
        #: error, because the two are the difference between a playlist moving
        #: on and a playlist being abandoned: both arrive as a stopped pipe with
        #: no error. See the MSG_PROCESS_FINISHED branch in start().
        self.ended = False
        # Which stages have reported what. Both are consulted before the next
        # stage is started, so one stage cannot start two.
        self._ready_seen = set()
        self._finished_seen = set()

    def status_msg(self):
        if self.started:
            return self.title
        else:
            idx = self.next_proc - 1
            if idx < 0:
                idx = 0
            return self.procs[idx].status_msg()

    def add_subtitles(self, chosen, lang=None):
        """Put subtitles on the film that is playing, if the player can."""
        if not self.started:
            return False
        return self.procs[-1].add_subtitles(chosen, lang)

    def waiting(self):
        """
        Whether the player is up but nothing has come through it yet.

        A source that has connected and gone quiet -- a torrent with no peers,
        a feed that accepted the connection and stalled -- looks exactly like a
        film from here: the last stage reported ready and the pipe is running.
        The player stage knows the difference, and the interface shows it as a
        count rather than as a title over a black screen.
        """
        if not self.started:
            return False
        return self.procs[-1].waiting()

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
                # A stage that has already reported -- ready once already, or
                # finished at all -- must not start another.
                #
                # It could: a stage reports ready, then finishes, and a
                # straggling ready arrives after the finish, from a read that
                # was already in flight when the process ended. Starting the
                # next stage again ran a second player over the same film.
                #
                # Two players is the whole problem, not untidiness. Only one of
                # them is a stage this pipe tracks, so only one is stopped; the
                # other keeps its picture and its sound, the pipe reports itself
                # finished, and the film on screen cannot be ended by anything
                # the interface sends. Reported from an IPTV stream on a Pi:
                # stop cleared the interface and the video carried on.
                if self.stopping:
                    # A ready that arrives after this pipe was stopped must not
                    # start anything.
                    #
                    # stop() can be called from outside, and when it is it does
                    # not end this loop -- it only tells the stages to die and
                    # reports the pipe finished to the player. The loop stays
                    # blocked here waiting for a message, and a stage that is
                    # still resolving can still deliver one.
                    #
                    # It happened on IPTV, and the gap is wide enough to hit by
                    # accident: the player asked for a second stream while the
                    # first was still resolving, so the pipe was stopped and
                    # reported finished, and then the resolver finished anyway
                    # and its late ready started a VLC for a pipe that had
                    # already let go of it. Nothing owned that VLC afterwards.
                    # One click, two players.
                    #
                    # The other VLC holds the DRM lease, so this one could not
                    # get one and retried a video output for ever:
                    #
                    #     drm_vout vout display error: Failed to get xlease
                    #
                    # at 96% CPU, with the interface showing one film playing.
                    #
                    # Checked before the stage bookkeeping below, because a
                    # stopped pipe starts nothing at all.
                    cherrypy.log("IGNORING READY AFTER STOP: " + name)
                elif idx in self._ready_seen or idx in self._finished_seen:
                    cherrypy.log("IGNORING SECOND READY: " + name)
                elif not self._is_last_proc(idx):
                    self._ready_seen.add(idx)
                    self._start_next(args)
                else:
                    self._ready_seen.add(idx)
                    self.started = True

            elif m == MSG_PROCESS_FINISHED:
                cherrypy.log("FINISHED: " + name)
                self._finished_seen.add(idx)
                if self._is_last_proc(idx):
                    # The last stage finishing without an error is the film
                    # ending. The player is the last stage, and VLC exits on its
                    # own when the media runs out, so this is the only path that
                    # means "it played to the end" rather than "something stopped
                    # it" -- a stage that failed halts instead, and a pipe
                    # stopped from outside is killing. What a playlist moves on
                    # from, so it is recorded rather than dropped by stop() a
                    # line below.
                    #
                    # Not for any other stage: a resolver finishing is the start
                    # of the work, not the end of it.
                    self.ended = True
                    self.stop()
                    break

            elif m == MSG_PROCESS_HALTED:
                cherrypy.log("HALTED: " + name)
                self.stop()
                break

    def _with_title(self, args):
        """
        The title travels with the args down the pipe.

        What is being played is known here and nowhere else -- ProcessPipe is
        constructed with it -- and the player stage would like it, to say what
        started once the film is up. Adding it to the args dict rather than
        setting it on the backend keeps build_command() pure, which is what lets
        it be tested without the player installed, and keeps the backend
        reusable between plays rather than carrying one film into the next.
        """
        if not args or "title" in args:
            return args
        return dict(args, title=self.title)

    def _last_proc(self):
        return self.procs[len(self.procs) - 1]

    def _is_last_proc(self, idx):
        return idx == len(self.procs) - 1

    def _start_next(self, args=None):
        """
        Hand the next stage its arguments, and start it.

        args is None rather than {} for a reason: a dict as a default is one
        dict for the life of the process, so every pipeline's first stage was
        being handed the same one. Whatever that stage wrote there -- the film's
        URL, and now the slave URL of a two-stream source -- was still there for
        the next film, which is how a muxed source came to be given the audio
        stream of the YouTube video played before it.
        """
        if args is None:
            args = {}
        if self.next_proc >= len(self.procs):
            # Nothing left to start. Every caller checks this first, so getting
            # here means a message arrived for a stage that does not exist --
            # not worth an IndexError out of the pipe loop.
            return
        proc = self.procs[self.next_proc]
        cherrypy.log("STARTING: " + proc.name() + ' "' + repr(proc) + '"')
        proc.set_msgq(self.msgq, self.next_proc)
        self.threads.append(_start_thread(proc.start, self._with_title(args)))
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
        self.pmsgq.put(self.ended)

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

    def waiting(self):
        """Whether this stage is up but has nothing to play yet."""
        return False

    def add_subtitles(self, chosen, lang=None):
        """Attach subtitles to a film already playing. Most players cannot."""
        return False

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
        # How long a single unterminated output line may grow before its tail is
        # kept instead. Most stages want the small default; yt-dlp raises this
        # because its dump-single-json output is one very long line.
        self.partial_limit = 2000
        # Extra environment for the child. None inherits ours.
        self.env = None
        self.killing = False
        # Set by start() when the command is spawned, and back to None once it
        # has been waited for. Declared here as well so that stop() is safe to
        # call on a stage that has never started: stopping a pipeline while the
        # first stage is still coming up does exactly that, and stop() reads
        # this attribute to decide whether there is anything to kill.
        self.proc = None
        if not os.path.exists(TMP_DIR):
            os.makedirs(TMP_DIR)

    def start(self, args):
        cmd = self._get_cmd(args)
        if self.killing:
            # Asked to stop before the process exists, so do not start it.
            #
            # _start_next() creates this thread and stop() joins it, so the gap
            # between the two is where this stage sits: created, not yet run.
            # stop() runs in that gap and finds self.proc is None, so there is
            # nothing to kill -- and then the join hands this thread the very
            # time it needed to get here, and it would spawn a process the
            # pipeline has already finished with.
            #
            # Nothing tracks that process. The pipe has sent
            # MSG_PLAYER_PIPE_STOPPED and the player loop has cleared its
            # handle, so no later stop can reach it, and it plays for ever. On
            # an IPTV stream that leaves a VLC holding the DRM lease, so the
            # next one cannot get it --
            #
            #     drm_vout vout display error: Failed to get xlease
            #
            # -- and retries a video output it will never get, once every half
            # second, at full CPU. Two players on screen, and the interface
            # reporting nothing playing. Reported from IPTV on a Pi.
            #
            # Checked here, immediately before the spawn, because that is the
            # last point at which there is still nothing to clean up.
            cherrypy.log("NOT STARTING, ALREADY STOPPED: " + self.name())
            return
        if isinstance(cmd, str):
            cherrypy.log("RUN: " + cmd)
        else:
            # str() every part: a stage that hands an int in its argv -- a year,
            # a file index -- is a bug, but it is not one that should kill the
            # stage while reporting the command. It did: the join raised out of
            # the thread, the pipeline never started, and the interface sat at
            # LOADING STREAM with nothing in the log but a traceback.
            cherrypy.log("RUN: " + " ".join(str(part) for part in cmd))
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
        self._tail = _LineTail(
            on_line=self._observe_line, partial_limit=self.partial_limit
        )
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

    def _terminate(self):
        """
        Stop the process, giving it the chance to tidy up first.

        SIGKILL alone is what stopped a second film from playing. VLC takes a
        DRM lease on the display when it opens a video output, and gives it up
        when it exits properly. Killed, it has no chance to release it, so the
        next one comes up, cannot get the lease --

            drm_vout vout display error: Failed to get xlease

        -- and then every frame fails to commit:

            Atomic commit failed: No space left on device

        which is the video driver running out of buffers and not the disk. The
        picture freezes on the last frame and nothing recovers it, which is what
        a stopped film followed by another one looked like. The message says
        "no space" and every check of the disk says there is plenty.

        So SIGTERM first, briefly, and only SIGKILL what is left. SIGTERM is not
        guaranteed to be honoured -- nothing is, from a process that has already
        wedged -- so the kill is still there, just no longer first.
        """
        proc = self.proc
        if proc is None:
            return
        try:
            # kill - including all children of process
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            # Not a reason to stop. killpg addresses a process *group*, and
            # the group is only there once the child has run the setsid it was
            # spawned with. Stopping a stage that is still starting can beat
            # that, and then the group named by this pid does not exist yet --
            # while the process itself very much does.
            pass
        try:
            proc.wait(timeout=_TERMINATE_GRACE)
            return
        except Exception:
            # The process did not leave within the grace period. Fall through
            # to the SIGKILL path below rather than giving up on it.
            pass
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        try:
            # By pid as well, because the group can be missing while the process
            # is not, which is the case above. kill() only reaches this process
            # and not anything it spawned, which is what the group was for, so
            # this is the fallback and not the replacement.
            proc.kill()
        except Exception:
            pass
        # Make sure what was asked for has actually happened. Without this the
        # process is a zombie until something reaps it, and the call site
        # seeing only _terminate() could conclude the stop failed. Sending
        # SIGKILL does not wait for it to take effect.
        try:
            proc.wait(timeout=_TERMINATE_GRACE)
        except Exception:
            pass

    def stop(self):
        # Always, and before the check for a process, not inside it.
        #
        # This flag is the stage's record of "you are being stopped", and the
        # only thing that can set it is this call. Setting it only when there was
        # a process to kill meant a stage stopped before it had one recorded
        # nothing at all -- so nothing knew it must not start, and start() went
        # on to spawn a process for a pipeline that had already finished with
        # it. See start().
        self.killing = True
        if self.proc is not None:
            # Before _terminate(), not after. _terminate() waits up to
            # _TERMINATE_GRACE for the process to leave, and _wait() runs in
            # another thread: with killing set afterwards, a process that exits
            # promptly during that wait was seen as having died on its own, and
            # every deliberate stop became an error in the UI --
            #
            #     Process exit: -15 | Verifying downloaded: 10%
            #
            # which is the opposite of what was asked for. SIGTERM by
            # definition makes it exit, so this is the one case that must be
            # marked before it can possibly be observed.
            self._terminate()

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

from queue import Full, Queue

import cherrypy

from ..settings import load
from . import ythelper
from .backends import DEFAULT_BACKEND, get_backend
from .dlsrvproc import DlsrvProcess
from .localproc import LocalFileProcess
from .pflixproc import PeerflixProcess
from .processpipe import MSG_PLAYER_PIPE_STOPPED, ProcessPipe, _start_thread
from .subsproc import SubtitlesProcess
from .ytdlproc import YoutubeDlProcess

# playRtmpdump() and playLivestream() were the only users of RtmpProcess and
# LivestreamerProcess, and nothing called either method. livestreamer in
# particular is no longer installable or supported upstream, and configure.sh
# never provisioned it. The methods and their two imports are commented out
# below rather than deleted so the wrappers stay recoverable.
# from .lvstrmrproc import LivestreamerProcess
# from .rtmpproc import RtmpProcess

ST_NOT_RUNNING = 0
ST_STARTING = 1
ST_RUNNING = 3

#: Capacity of the player loop's message queue. A play costs two messages, so a
#: capacity of two -- which is what this used to be -- held exactly one play.
#: If the loop stalled, the next play filled the queue and the one after that
#: blocked forever inside the request thread, so the API stopped responding
#: instead of reporting a problem.
_PLAY_QUEUE_SIZE = 32

#: Seconds to wait to hand a play to the loop before giving up.
_PLAY_ENQUEUE_TIMEOUT = 5

#: Seconds to wait for a stopped pipeline's thread to finish before going on
#: without it. Bounded because this join is on the player loop itself: a
#: pipeline that will not unwind -- a VLC wedged in demuxing, which ignores the
#: quit its cli was sent -- left it joining forever, and a loop that never comes
#: back reads every later stop and play as no answer at all. The whole
#: interface went unresponsive while the film was still on screen.
_PLAY_JOIN_TIMEOUT = 5

MSG_PLAYER_PLAY = 1
MSG_PLAYER_STOP = 2
MSG_PLAYER_QUIT = 3


class _Player:
    def __init__(self):
        self.msgq = Queue(_PLAY_QUEUE_SIZE)
        self.play_pipe = None
        self.play_thread = None
        self.main_thread = None
        self.error = None
        self.paused = False

    def _stop(self):
        if self.play_pipe:
            self.play_pipe.stop()

    def _play(self, pipe):
        self.error = None
        self.paused = False
        self.play_pipe = pipe
        self.play_thread = _start_thread(pipe.start, self.msgq)

    def _is_playing(self):
        if self.play_pipe is not None and self.play_pipe.is_started():
            return True
        else:
            return False

    def _is_stopping(self):
        if self.play_pipe is not None and self.play_pipe.is_stopping():
            return True
        else:
            return False

    def start(self):
        msgq = self.msgq
        nextpipe = None
        while True:
            m = msgq.get()
            # Nothing in here may be allowed to end this thread.
            #
            # This is the whole player. Every stop, every play and every
            # status the interface shows is answered from this loop, so an
            # exception escaping it does not fail the one request that caused
            # it -- it leaves the player permanently deaf. Nothing the user
            # does afterwards reaches anything: stop returns 200 and no film
            # stops, play returns 200 and nothing starts, and the film on
            # screen is still there with no way to end it.
            #
            # It happened: stopping a pipeline whose first stage was still
            # starting raised AttributeError out of stop(), which ended this
            # thread, and the player was unusable until the process was
            # restarted. The message is reported and the loop carries on, so a
            # single bad message costs one action rather than all of them.
            try:
                if m == MSG_PLAYER_PLAY:
                    if self.play_pipe is not None:
                        nextpipe = msgq.get()
                        self._stop()
                    else:
                        self._play(msgq.get())
                elif m == MSG_PLAYER_STOP:
                    self._stop()
                elif m == MSG_PLAYER_PIPE_STOPPED:
                    self.error = msgq.get()
                    # Bounded, and the state is cleared either way. Waiting on a
                    # thread that may never finish left the loop unable to answer
                    # anything, which is worse than a thread still winding down in
                    # the background: it cannot stop the film, and it cannot start
                    # the next one either.
                    self.play_thread.join(timeout=_PLAY_JOIN_TIMEOUT)
                    if self.play_thread.is_alive():
                        cherrypy.log("PLAY THREAD DID NOT EXIT")
                    cherrypy.log("PIPE STOPPED")
                    if nextpipe is not None:
                        self._play(nextpipe)
                        nextpipe = None
                    else:
                        self.play_pipe = None
                        self.play_thread = None
                elif m == MSG_PLAYER_QUIT:
                    self._stop()
                    break
            except Exception as exc:
                cherrypy.log("PLAYER LOOP ERROR on message %r: %s" % (m, exc))

    def quit(self):
        if self.main_thread is not None:
            self.msgq.put(MSG_PLAYER_QUIT)
            self.main_thread.join()

    def _player_stage(self, http, dlsrv):
        """
        Return the backend stage for a pipeline.

        A backend named in settings wins, defaulting to DEFAULT_BACKEND.
        """
        return get_backend(load("player").get("backend", DEFAULT_BACKEND))

    def play(self, title, src, subs=None, http=False, dlsrv=True):
        if self.main_thread is None:
            self.main_thread = _start_thread(self.start)
        pipe = ProcessPipe(title)
        if subs is not None:
            pipe.add_process(SubtitlesProcess(subs))
        pipe.add_process(src)
        if not http and dlsrv:
            # The file is still growing, so it is served over http rather than
            # piped, which is what lets the player follow it.
            pipe.add_process(DlsrvProcess())
        pipe.add_process(self._player_stage(http, dlsrv))
        self._enqueue(MSG_PLAYER_PLAY, pipe)

    def _enqueue(self, *items):
        """
        Hand items to the player loop, refusing to wait forever.

        Every caller of play() is a CherryPy request thread, so a blocking put
        here would take the whole web server down rather than just fail one
        request. A full queue means the loop is not draining messages, which is
        the wedge this whole path is guarding against, so it is reported rather
        than absorbed.

        Cleared here rather than in _play() so that a failed attempt does not
        leave the previous attempt's error on screen. That error used to persist
        for as long as the loop was wedged, and read as though it described the
        play that had just been requested.
        """
        self.error = None
        for item in items:
            try:
                self.msgq.put(item, timeout=_PLAY_ENQUEUE_TIMEOUT)
            except Full:
                self.error = (
                    "Player is not responding (%d requests queued already)"
                    % _PLAY_QUEUE_SIZE
                )
                cherrypy.log(self.error)
                raise RuntimeError(self.error)

    def playYtdl(self, url, title=None, subs=None):
        if title is None:
            title = url
        # True means yt-dlp should only resolve the stream (--simulate), so the
        # bytes are piped straight to the player instead of buffered to disk.
        pipe_directly = ythelper.skip_download(url)
        self.play(title, YoutubeDlProcess(url), subs, pipe_directly)

    # Dead: RtmpProcess is only reachable from here, and rtmpdump is not
    # installed by configure.sh.
    # def playRtmpdump(self, cmd, title):
    #     self.play(title, RtmpProcess(cmd))

    def playTorrent(self, url, idx, title, subs):
        self.play(title, PeerflixProcess(url, idx), subs, True)

    # Dead: LivestreamerProcess is only reachable from here, and livestreamer is
    # no longer supported upstream and is not installed by configure.sh.
    # def playLivestream(self, url, title):
    #     self.play(title, LivestreamerProcess(url), http=False, dlsrv=False)

    def playLocalFile(self, filepath, title):
        # Local File path behaviour is like http
        self.play(title, LocalFileProcess(filepath), http=True)

    def status(self):
        play_pipe = self.play_pipe
        status = {
            "State": ST_NOT_RUNNING,
            "Msg": "",
            "Title": "",
            "Paused": False,
            "Error": False,
        }

        if self.error is not None:
            status["Error"] = True
            status["Msg"] = self.error

        if play_pipe is None or self.error is not None:
            return status

        status["Title"] = play_pipe.status_msg()
        if self._is_playing():
            status["State"] = ST_RUNNING
        elif not self._is_stopping():
            status["State"] = ST_STARTING
        status["Paused"] = self.paused

        return status

    def control(self, action):
        if action == "stop":
            self.msgq.put(MSG_PLAYER_STOP)
        elif self._is_playing():
            self.play_pipe.control(action)
            if action in ("pause", "resume"):
                # Toggled here for the same reason the player toggles it: which
                # of the two words arrived does not say what the player did.
                # Both buttons send "pause", so the interface's own idea of the
                # state is the one thing that cannot be trusted to describe
                # what happened -- and it is what decides which button is drawn
                # and what the spacebar sends next.
                self.paused = not self.paused


Player = _Player()

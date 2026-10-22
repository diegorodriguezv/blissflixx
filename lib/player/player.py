import time
from collections import namedtuple
from queue import Full, Queue

import cherrypy

from ..settings import load
from . import ythelper
from .backends import DEFAULT_BACKEND, get_backend
from .dlsrvproc import DlsrvProcess
from .localproc import LocalFileProcess
from .pflixproc import PeerflixProcess
from .processpipe import MSG_PLAYER_PIPE_STOPPED, ProcessPipe, _start_thread
from .subsproc import ChosenSubtitlesProcess
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
#: Move to the neighbouring item of the playlist this film came from. The
#: direction follows as its own message, the way a play is followed by its pipe.
MSG_PLAYER_SKIP = 5

#: One queued play: the pipeline to start, and where it sits in a playlist when
#: it came from one.
#:
#: One object rather than a message each, for two reasons. A pipe and a playlist
#: must never be paired with different films -- a play arriving while another is
#: ending is exactly when that could happen -- and a play has to cost the two
#: queue slots that _PLAY_QUEUE_SIZE is sized on, so that a stalled loop still
#: refuses a play rather than blocking the request thread on the second half of
#: one.
_Play = namedtuple("_Play", "pipe playlist index")


class _Player:
    def __init__(self):
        self.msgq = Queue(_PLAY_QUEUE_SIZE)
        self.play_pipe = None
        self.play_thread = None
        self.main_thread = None
        self.error = None
        self.paused = False
        #: Where the film playing came from, when it came from a playlist: the
        #: list of builders for every item, and which one this is. The list goes
        #: with the film rather than living here, so a play asked for while this
        #: one is ending cannot make the wrong list advance -- see _play().
        self.playlist = None
        self.index = None
        #: Something to say that is not a failure of the play in progress: an
        #: item skipped, or the end of a playlist reached. Kept apart from
        #: self.error because that one means nothing is playing -- see status().
        self.notice = None
        #: When the film playing was asked for. The interface counts from here
        #: while it is loading; see status().
        self.play_requested_at = None
        #: Whether the start has been timed into the log yet.
        self.start_logged = False

    def _stop(self):
        if self.play_pipe:
            self.play_pipe.stop()

    def _play(self, play):
        self.error = None
        self.paused = False
        self.play_pipe = play.pipe
        self.playlist = play.playlist
        self.index = play.index
        # When this film was asked for, for the count the interface shows while
        # it is loading. Wall clock rather than a tick, because the loop is not
        # the thing being timed.
        self.play_requested_at = time.time()
        self.start_logged = False
        self.play_thread = _start_thread(play.pipe.start, self.msgq)

    def _clear_place(self):
        """
        Forget which playlist this film belonged to.

        What ends a run: the films after this one stop being lined up, and the
        film playing stops being one of a series.
        """
        self.playlist = None
        self.index = None

    def _neighbour_index(self, delta):
        """
        Where the neighbouring item is, or None when there is not one.

        None covers both the end of the list and a film that is not in a
        playlist at all, which are the same thing from here.
        """
        if self.playlist is None or self.index is None:
            return None
        target = self.index + delta
        if target < 0 or target >= len(self.playlist):
            return None
        return target

    def _adjacent(self, delta):
        """
        The neighbouring item as a play, or None when there is not one.

        Built rather than enqueued because the caller is the loop itself, so it
        is handed to _play() exactly as a play that arrived on the queue is.
        """
        index = self._neighbour_index(delta)
        if index is None:
            return None
        return _Play(self.playlist[index](), self.playlist, index)

    def _no_neighbour(self, delta):
        """Why there is nothing to skip to, in words."""
        if self.playlist is None:
            return "Not playing a playlist"
        return "End of playlist" if delta > 0 else "Start of playlist"

    def _after(self, title, ended):
        """
        What plays next, if anything, once a film has stopped.

        A film that ended moves a playlist on, and so does one that failed, so
        that a single dead item cannot end a run. The failure is said out loud,
        because moving past a film silently is worse than not moving past it.

        Anything else ends the run: a stop has already dropped the place by the
        time it arrives here, and so has a play that replaced this one -- that
        one is waiting in front of this, and the loop plays it instead.
        """
        index = self._neighbour_index(1)
        if index is None or not (ended or self.error):
            self.play_pipe = None
            self.play_thread = None
            self._clear_place()
            return
        skipped = self.error
        self._play(_Play(self.playlist[index](), self.playlist, index))
        if skipped:
            self.notice = "Skipped %s: %s" % (title, skipped)

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
        nextplay = None
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
                    play = msgq.get()
                    if self.play_pipe is not None:
                        nextplay = play
                        self._stop()
                    else:
                        self._play(play)
                elif m == MSG_PLAYER_STOP:
                    # Stop means stop. Whatever was lined up behind this film
                    # goes with it, and so does its place in a playlist --
                    # otherwise pressing stop is followed by a film that was
                    # already queued, which is not what the button says.
                    nextplay = None
                    self.notice = None
                    self._stop()
                    self._clear_place()
                elif m == MSG_PLAYER_SKIP:
                    delta = msgq.get()
                    play = self._adjacent(delta)
                    if play is None:
                        self.notice = self._no_neighbour(delta)
                    elif self.play_pipe is not None:
                        nextplay = play
                        self._stop()
                    else:
                        self._play(play)
                elif m == MSG_PLAYER_PIPE_STOPPED:
                    self.error = msgq.get()
                    ended = msgq.get()
                    title = self.play_pipe.title if self.play_pipe is not None else ""
                    # Bounded, and the state is cleared either way. Waiting on a
                    # thread that may never finish left the loop unable to answer
                    # anything, which is worse than a thread still winding down in
                    # the background: it cannot stop the film, and it cannot start
                    # the next one either.
                    self.play_thread.join(timeout=_PLAY_JOIN_TIMEOUT)
                    if self.play_thread.is_alive():
                        cherrypy.log("PLAY THREAD DID NOT EXIT")
                    cherrypy.log("PIPE STOPPED")
                    if nextplay is not None:
                        self._play(nextplay)
                        nextplay = None
                    else:
                        self._after(title, ended)
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

    def build(self, title, src, subs=None, http=False, dlsrv=True):
        """
        Assemble a pipeline without starting it.

        Split out from play() so that a pipe can be built somewhere that must
        not queue one: the loop builds a playlist's next item while it is
        draining the queue it reads, and play() from there would be a message
        the loop is waiting to hand to itself.
        """
        pipe = ProcessPipe(title)
        # Subtitles are never searched for on the way to a film. OpenSubtitles is
        # metered, and a search that runs because somebody pressed play is a
        # call nobody asked for -- it failed once in the middle of a play and
        # put its error on screen. The only search is the one made from the
        # subtitle page, which comes here as the ids that were picked.
        #
        # A torrent's own subtitle files are not searched for either: they come
        # out of the torrent itself, in the stage that has it open. See
        # buildTorrent.
        if subs is not None and subs.get("chosen"):
            pipe.add_process(ChosenSubtitlesProcess(subs))
        pipe.add_process(src)
        if not http and dlsrv:
            # The file is still growing, so it is served over http rather than
            # piped, which is what lets the player follow it.
            pipe.add_process(DlsrvProcess())
        pipe.add_process(self._player_stage(http, dlsrv))
        return pipe

    def playPipe(self, pipe, playlist=None, index=None):
        """
        Queue a pipeline that has already been built.

        playlist and index travel with the pipe rather than being kept here, so
        the list that advances is the one this film came from even if another
        play arrives while it is ending.
        """
        if self.main_thread is None:
            self.main_thread = _start_thread(self.start)
        self._enqueue(MSG_PLAYER_PLAY, _Play(pipe, playlist, index))

    def play(
        self, title, src, subs=None, http=False, dlsrv=True, playlist=None, index=None
    ):
        self.playPipe(self.build(title, src, subs, http, dlsrv), playlist, index)

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

        The notice goes with it, for the same reason: asking for something is
        what makes what happened to the last film stop being news.
        """
        self.error = None
        self.notice = None
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

    def playYtdl(self, url, title=None, subs=None, playlist=None, index=None):
        self.playPipe(self.buildYtdl(url, title, subs), playlist, index)

    def buildYtdl(self, url, title=None, subs=None):
        if title is None:
            title = url
        # True means yt-dlp should only resolve the stream (--simulate), so the
        # bytes are piped straight to the player instead of buffered to disk.
        return self.build(
            title, YoutubeDlProcess(url), subs, ythelper.skip_download(url)
        )

    # Dead: RtmpProcess is only reachable from here, and rtmpdump is not
    # installed by configure.sh.
    # def playRtmpdump(self, cmd, title):
    #     self.play(title, RtmpProcess(cmd))

    def playTorrent(self, url, idx, title, subs, playlist=None, index=None):
        self.playPipe(self.buildTorrent(url, idx, title, subs), playlist, index)

    def buildTorrent(self, url, idx, title, subs):
        # subs names the files inside this torrent when it carries any: the
        # torrent stage fetches them from the swarm while the film starts, and
        # the player adds them mid-play. Both shapes are accepted -- a list from
        # a row ("the subtitles it has") and a single index from an older play.
        wanted = (subs or {}).get("torrent") or {}
        files = wanted.get("files")
        if files is None and wanted.get("idx") is not None:
            files = [{"idx": wanted["idx"], "name": wanted.get("name")}]
        return self.build(title, PeerflixProcess(url, idx, files or []), subs, True)

    # Dead: LivestreamerProcess is only reachable from here, and livestreamer is
    # no longer supported upstream and is not installed by configure.sh.
    # def playLivestream(self, url, title):
    #     self.play(title, LivestreamerProcess(url), http=False, dlsrv=False)

    def playLocalFile(self, filepath, title, playlist=None, index=None):
        self.playPipe(self.buildLocalFile(filepath, title), playlist, index)

    def buildLocalFile(self, filepath, title):
        # Local File path behaviour is like http
        return self.build(title, LocalFileProcess(filepath), http=True)

    def status(self):
        play_pipe = self.play_pipe
        status = {
            "State": ST_NOT_RUNNING,
            "Msg": "",
            "Title": "",
            "Paused": False,
            "Error": False,
            # Something said about a film other than the one playing -- an item
            # skipped, or the end of a playlist reached. Apart from Msg, which
            # along with Error means nothing is playing at all.
            "Notice": "",
            # Whether the player is up but nothing has come through it yet, and
            # how long since the film was asked for. See ProcessPipe.waiting():
            # the interface shows these as "starting 12s" rather than a title
            # that sits there looking like a film with a black screen.
            "Waiting": False,
            "Elapsed": 0,
        }

        if self.error is not None:
            status["Error"] = True
            status["Msg"] = self.error

        if self.notice is not None:
            status["Notice"] = self.notice

        if play_pipe is None or self.error is not None:
            return status

        if self.play_requested_at is not None:
            status["Elapsed"] = int(time.time() - self.play_requested_at)

        status["Title"] = play_pipe.status_msg()
        status["Waiting"] = play_pipe.waiting()
        # How long it took, once, for the question that keeps coming back:
        # "was that slower than it used to be". Everything a play waits on is
        # already logged with a timestamp -- peerflix verifying its buffer,
        # yt-dlp resolving, the player opening -- and this is the total.
        if (
            not self.start_logged
            and self.play_requested_at is not None
            and play_pipe.is_started()
            and not status["Waiting"]
        ):
            self.start_logged = True
            cherrypy.log(
                "PLAY STARTED after %.1fs: %s"
                % (time.time() - self.play_requested_at, status["Title"])
            )
        if self._is_playing():
            status["State"] = ST_RUNNING
        elif not self._is_stopping():
            status["State"] = ST_STARTING
        status["Paused"] = self.paused

        return status

    def control(self, action):
        if action == "stop":
            self.msgq.put(MSG_PLAYER_STOP)
        elif action in ("next_item", "prev_item"):
            # Not forwarded to the pipe: there is nothing the film can do about
            # it, it is a different film. Queued like any other play, so the loop
            # does all the stopping and starting, in the order they arrived.
            self._enqueue(MSG_PLAYER_SKIP, 1 if action == "next_item" else -1)
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

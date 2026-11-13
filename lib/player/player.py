import os
import time
from collections import namedtuple
from queue import Full, Queue

import cherrypy

from .. import playquality
from ..locations import DATA_PATH
from ..settings import load
from . import ythelper
from .backends import DEFAULT_BACKEND, get_backend
from .dlsrvproc import DlsrvProcess
from .localproc import LocalFileProcess
from .ltproc import LibtorrentProcess
from .processpipe import MSG_PLAYER_PIPE_STOPPED, ProcessPipe, _start_thread
from .sourcecheck import SourceCheck
from .subsproc import ChosenSubtitlesProcess
from .ytdlproc import YoutubeDlProcess

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

#: How long a live picture may be starved of input before the player moves the
#: channel to another source. The bar warns at fifteen seconds; twenty gives
#: the stream a little longer to come back by itself before the screen changes.
_RECONNECT_AFTER = 20

#: How long to wait for the parallel source check after a source fails before
#: the picture: a second or two can save trying an address that is already
#: known to be dead, without becoming a timeout of its own.
_PROBE_WAIT = 5

MSG_PLAYER_PLAY = 1
MSG_PLAYER_STOP = 2
MSG_PLAYER_QUIT = 3
#: How long the picture may stand still before the bar says the stream has
#: stalled. Long enough that a seek, or a keyframe the decoder is slow to
#: produce, is not mistaken for a dead stream.
_STALL_PICTURE_AFTER = 15
#: How long the swarm may have no peers while the file is still incomplete
#: before the bar says so. Earlier than the picture warning on purpose: this is
#: the one that arrives while there is still something to watch.
_STALL_SOURCE_AFTER = 30
#: How long after a pause/resume command the interface's own idea of the state
#: is trusted before the player's is. See _Player._sync_paused.
_PAUSE_SETTLE = 2.0#: Move to the neighbouring item of the playlist this film came from. The
#: direction follows as its own message, the way a play is followed by its pipe.
MSG_PLAYER_SKIP = 5
#: The live picture has been frozen long enough: try another source. Followed by
#: the play generation it belongs to, so a message that outlives its play is
#: ignored rather than switching the next one.
MSG_PLAYER_RECONNECT = 6

#: One queued play: the pipeline to start, and where it sits in a playlist when
#: it came from one.
#:
#: One object rather than a message each, for two reasons. A pipe and a playlist
#: must never be paired with different films -- a play arriving while another is
#: ending is exactly when that could happen -- and a play has to cost the two
#: queue slots that _PLAY_QUEUE_SIZE is sized on, so that a stalled loop still
#: refuses a play rather than blocking the request thread on the second half of
#: one.
_Play = namedtuple(
    "_Play", "pipe playlist index note keep_state", defaults=(None, False)
)


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
        #: The last position the player reported and when it last changed, for
        #: the stall warning: a picture that demonstrates it is moving is the
        #: one thing a stuck stream cannot fake. Reset per play.
        self._pos_last = None
        self._pos_changed_at = None
        #: When the pause state was last asked for by the interface. See
        #: _sync_paused: the player's answer wins after a moment.
        self._paused_at = 0.0
        #: When the source now being tried started, for the quality journal.
        #: Not the same as play_requested_at: a failover must not make the
        #: working mirror look as slow as the attempts before it.
        self.attempt_started_at = None
        #: Whether the stall already reported for this attempt was recorded.
        self._stall_logged = False
        #: "source 2 of 12", while a channel is failing over.
        self._failover_note = None
        #: Whether the pipe being stopped was stopped on purpose. A stop reads
        #: like a failure at the pipe -- it reports any stage error -- and a
        #: stop must not be recorded, or failed over.
        self._stopped = False
        #: The parallel check of this channel's unrated sources, and the URLs
        #: this play has already tried. Both last for one play.
        self._check = None
        self._tried = set()
        #: One reconnect request per stalled attempt, and a generation so a
        #: request that outlives its play cannot switch the next one.
        self._reconnect_sent = False
        self._play_seq = 0
        self._attempt_seq = 0
        #: The play that has been asked for and not started yet: (pipe, when,
        #: came from a playlist). The loop may still be stopping the old pipe,
        #: which can take seconds; the bar shows this instead of going blank.
        self.pending = None

    def _stop(self):
        if self.play_pipe:
            self.play_pipe.stop()

    def _play(self, play, requested_at=None):
        continuation = bool(getattr(play, "keep_state", False))
        pending_at = None
        if self.pending is not None and self.pending[0] is play.pipe:
            pending_at = self.pending[1]
            self.pending = None
        self.error = None
        self.paused = False
        self.play_pipe = play.pipe
        self.playlist = play.playlist
        self.index = play.index
        # When this film was asked for, for the count the interface shows while
        # it is loading. Wall clock rather than a tick, because the loop is not
        # the thing being timed. A failover keeps the original moment: the
        # person has been waiting since the click, not since the retry. A
        # reconnect after a freeze starts a fresh count: they were watching,
        # and this wait is new.
        self.play_requested_at = (
            time.time() if requested_at is None else requested_at
        )
        if pending_at is not None and requested_at is None:
            # The count starts when the person clicked, not when the old pipe
            # finally let go of the display.
            self.play_requested_at = pending_at
        # Per attempt, so the quality journal's "started in Ns" is about this
        # source rather than about the failed ones before it.
        self.attempt_started_at = time.time()
        self.start_logged = False
        self._pos_last = None
        self._pos_changed_at = None
        self._stall_logged = False
        self._failover_note = getattr(play, "note", None)
        self._stopped = False
        self._reconnect_sent = False
        self._attempt_seq += 1
        if not continuation:
            # A new play: new sources, a new check, and a generation so a
            # reconnect request from the old one cannot touch this one.
            self._play_seq += 1
            self._stop_check()
            self._tried = set()
            self._check = None
            self._start_check()
        current = self._current_url()
        if current:
            self._tried.add(current)
        self.play_thread = _start_thread(play.pipe.start, self.msgq)

    def _stop_check(self):
        if self._check is not None:
            self._check.cancel()
            self._check = None

    def _ordered_sources(self, sources):
        """
        The row's sources in the order this box's evidence says to try them.

        The feed may be a stale copy: the browser keeps a channel's rows for a
        minute, so a source that just ended can still be listed first. The
        journal is current, so the first attempt is chosen from it -- a source
        that worked here, then the untried, then the ones that failed. The
        channel's own order stands inside each group, which keeps its quality
        order intact.
        """
        groups = {0: [], 1: [], 2: []}
        for source in sources:
            if not source:
                continue
            real = playquality.latest(source)
            if real and real[0] != "ok":
                group = 2
            elif real:
                group = 0
            else:
                group = 1
            groups[group].append(source)
        return groups[0] + groups[1] + groups[2]

    def _start_check(self):
        """
        Probe this channel's other unrated sources, while the first one plays.

        Only the unrated ones: a source with a recent real play already has
        better evidence than a playlist fetch. Nothing waits on this.
        """
        pipe = self.play_pipe
        if pipe is None or not getattr(pipe, "track", False):
            return
        sources = getattr(pipe, "sources", None) or []
        current = self._current_url()
        unrated = [
            url
            for url in sources
            if url and url != current and playquality.latest(url) is None
        ]
        if len(sources) > 1 and unrated:
            cherrypy.log(
                "SOURCE CHECK: %d unrated of %d sources" % (len(unrated), len(sources))
            )
            self._check = SourceCheck(unrated)

    def _current_url(self):
        """The URL being tried now, when the row carried a list of them."""
        pipe = self.play_pipe
        if pipe is None:
            return None
        sources = getattr(pipe, "sources", None) or []
        index = getattr(pipe, "source_index", 0)
        if 0 <= index < len(sources):
            return sources[index]
        return None

    def _media_time(self):
        """When the player first decoded a frame, from a stage that knows."""
        for stage in getattr(self.play_pipe, "procs", None) or []:
            media_at = getattr(stage, "_media_at", None)
            if media_at is not None:
                return media_at
        return None

    def _note_picture(self):
        """Write down how long this source took to show a picture."""
        pipe = self.play_pipe
        if pipe is None or not getattr(pipe, "track", False):
            return
        url = self._current_url()
        media_at = self._media_time()
        if not url or media_at is None or self.attempt_started_at is None:
            return
        playquality.record_ok(url, media_at - self.attempt_started_at)
        # The failed attempts before this one are no longer the news.
        self._failover_note = None

    def _pick_source(self, sources):
        """
        The next source to try, in the order the channel handed them over.

        The channel orders the sources by quality first, then by what worked,
        with the ones that failed here last. The player only moves the probed
        dead and the already-failed after the rest; within those groups the
        channel's order stands, which is what keeps a 1080p source ahead of a
        720p one. Nothing is skipped: the last bad source is still tried after
        every better one.
        """
        options = []
        for index, url in enumerate(sources):
            if not url or url in self._tried:
                continue
            real = playquality.latest(url)
            probe = self._check.result(url) if self._check is not None else None
            if real and real[0] != "ok":
                tier = 2
            elif real is None and probe is False:
                tier = 1
            else:
                tier = 0
            options.append((tier, index, url))
        if not options:
            return None
        tier, _index, url = min(
            options, key=lambda option: (option[0], option[1])
        )
        return tier, url

    def _next_source(self, wait_for_probe=False):
        """
        The next source to try, or None when the list is exhausted.

        After an instant failure the best remaining source may still be in
        flight in the probe; wait a little for it rather than spend the same
        seconds on a dead address. At most five seconds, and only when the
        chosen source has no real play yet and the probe has not answered.
        """
        pipe = self.play_pipe
        if pipe is None:
            return None
        sources = getattr(pipe, "sources", None) or []
        picked = self._pick_source(sources)
        if picked is None:
            return None
        tier, url = picked
        if wait_for_probe and tier == 0 and self._check is not None:
            real = playquality.latest(url)
            if (
                real is None
                and self._check.result(url) is None
                and not self._check.done()
            ):
                self._check.wait(_PROBE_WAIT)
                picked = self._pick_source(sources)
        return picked[1] if picked else None

    def _fail_over(self, title):
        """
        Try the next source of the same row, after this one failed to start.

        Only before the picture. A stream that played and then died is handled
        by _reconnect_play, which is allowed to restart it because a live
        channel has nothing to lose; a film would lose its place. Only for rows
        that carried a list -- a torrent has one address and a YouTube row has
        one URL. No attempt cap: the person chose the channel and does not care
        which source answers.
        """
        pipe = self.play_pipe
        if pipe is None or not getattr(pipe, "track", False):
            return False
        if self._picture_after() is not None:
            return False
        sources = getattr(pipe, "sources", None) or []
        index = getattr(pipe, "source_index", 0)
        failed = sources[index] if 0 <= index < len(sources) else None
        next_url = self._next_source(wait_for_probe=True)
        if not next_url:
            return False
        if failed:
            playquality.record_failed(failed, self.error)
        note = "retrying %d/%d" % (len(self._tried) + 1, len(sources))
        cherrypy.log("FAILOVER: %s, %s" % (title, note))
        newpipe = self.buildYtdl(
            next_url,
            title,
            getattr(pipe, "subs", None),
            sources,
            choose_first=False,
        )
        self._play(
            _Play(newpipe, self.playlist, self.index, note, True),
            requested_at=self.play_requested_at,
        )
        return True

    def _reconnect_play(
        self,
        title,
        allow_stopped=False,
        allow_unstarted=False,
        record_end=False,
    ):
        """
        Another source for a live channel that stopped showing its picture.

        The freeze is recorded by status() once the input has been idle long
        enough. This builds the next attempt; the loop stops the frozen pipe
        and starts this one. A stream still waiting for its first picture is not
        a freeze -- the stage's own timeout ends that -- and a film is never
        restarted, because that would throw away where the person was.

        A live stream that ends on its own is the other way this happens; VLC
        reports it as "end of playlist, exiting", which is not an ending for a
        live channel. That path records the source as ended and also tries the
        next one.
        """
        if not allow_stopped and self._is_stopping():
            return None
        pipe = self.play_pipe
        if pipe is None or not getattr(pipe, "track", False):
            return None
        ask = getattr(pipe, "is_live", None)
        if not callable(ask) or not ask():
            return None
        if not allow_unstarted and self._picture_after() is None:
            return None
        if record_end:
            ended_url = self._current_url()
            if ended_url:
                playquality.record_failed(ended_url, "Stream ended")
        sources = getattr(pipe, "sources", None) or []
        next_url = self._next_source(wait_for_probe=False)
        if not next_url:
            return None
        note = "retrying %d/%d" % (len(self._tried) + 1, len(sources))
        cherrypy.log(
            "%s: %s, %s"
            % ("STREAM ENDED" if record_end else "RECONNECT", title, note)
        )
        newpipe = self.buildYtdl(
            next_url,
            title,
            getattr(pipe, "subs", None),
            sources,
            choose_first=False,
        )
        return _Play(newpipe, self.playlist, self.index, note, True)

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
            self._stop_check()
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
                    self.pending = None
                    self._stopped = True
                    self._stop()
                    self._clear_place()
                elif m == MSG_PLAYER_SKIP:
                    delta = msgq.get()
                    self.pending = None
                    play = self._adjacent(delta)
                    if play is not None:
                        self.pending = (
                            play.pipe,
                            time.time(),
                            play.playlist is not None,
                        )
                    if play is None:
                        self.notice = self._no_neighbour(delta)
                    elif self.play_pipe is not None:
                        nextplay = play
                        self._stop()
                    else:
                        self._play(play)
                elif m == MSG_PLAYER_RECONNECT:
                    seq = msgq.get()
                    attempt = msgq.get()
                    if seq == self._play_seq and attempt == self._attempt_seq:
                        title = (
                            self.play_pipe.title
                            if self.play_pipe is not None
                            else ""
                        )
                        play = self._reconnect_play(title)
                        if play is not None:
                            nextplay = play
                            self._stop()
                elif m == MSG_PLAYER_PIPE_STOPPED:
                    nextplay = self._pipe_stopped(nextplay)
                elif m == MSG_PLAYER_QUIT:
                    self.pending = None
                    self._stopped = True
                    self._stop()
                    break
            except Exception as exc:
                cherrypy.log("PLAYER LOOP ERROR on message %r: %s" % (m, exc))

    def _pipe_stopped(self, nextplay):
        """
        The pipe stopped: play the queued one, fail over, or move on.

        A stop reports any stage error, so an error here is not enough to call
        the source broken: the person may have pressed stop or skip while it
        was winding down. Only an error nobody asked for can fail over.
        """
        self.error = self.msgq.get()
        ended = self.msgq.get()
        title = self.play_pipe.title if self.play_pipe is not None else ""
        # Bounded, and the state is cleared either way. Waiting on a thread
        # that may never finish left the loop unable to answer anything, which
        # is worse than a thread still winding down in the background: it
        # cannot stop the film, and it cannot start the next one either.
        self.play_thread.join(timeout=_PLAY_JOIN_TIMEOUT)
        if self.play_thread.is_alive():
            cherrypy.log("PLAY THREAD DID NOT EXIT")
        cherrypy.log("PIPE STOPPED")
        intentional = nextplay is not None or self._stopped
        self._stopped = False
        if nextplay is not None:
            self._play(nextplay)
        elif self.error and not intentional:
            if self._fail_over(title):
                pass
            else:
                failed = self._current_url()
                if failed and getattr(self.play_pipe, "track", False):
                    playquality.record_failed(failed, self.error)
                self._after(title, ended)
        elif ended and not intentional:
            # A live stream that ends has not ended for the person; VLC calls
            # it "end of playlist, exiting". Try the next source, and only move
            # on when there is none.
            play = self._reconnect_play(
                title,
                allow_stopped=True,
                allow_unstarted=True,
                record_end=True,
            )
            if play is not None:
                self._play(play)
            else:
                self._after(title, ended)
        else:
            self._after(title, ended)
        return None

    def quit(self):
        if self.main_thread is not None:
            self.msgq.put(MSG_PLAYER_QUIT)
            self.main_thread.join()
        self._stop_check()

    def _player_stage(self, http, dlsrv):
        """Return the player stage for a pipeline."""
        return get_backend(DEFAULT_BACKEND)

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
        # What a failover would rebuild this play with, when the source is a
        # stream with mirrors. Kept on the pipe so a playlist item carries it
        # too; see buildYtdl.
        pipe.subs = subs
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
        self.pending = (pipe, time.time(), playlist is not None)
        try:
            self._enqueue(MSG_PLAYER_PLAY, _Play(pipe, playlist, index))
        except RuntimeError:
            self.pending = None
            raise

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
        self._failover_note = None
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

    def playYtdl(
        self, url, title=None, subs=None, playlist=None, index=None, sources=None
    ):
        self.playPipe(self.buildYtdl(url, title, subs, sources), playlist, index)

    def buildYtdl(
        self, url, title=None, subs=None, sources=None, choose_first=True
    ):
        if sources:
            sources = list(sources)
            if choose_first:
                # The row may have been built before the last play; the
                # journal knows which of its sources actually works now.
                sources = self._ordered_sources(sources)
                url = sources[0]
        if title is None:
            title = url
        # True means yt-dlp should only resolve the stream (--simulate), so the
        # bytes are piped straight to the player instead of buffered to disk.
        pipe = self.build(
            title, YoutubeDlProcess(url), subs, ythelper.skip_download(url)
        )
        # A row that carried mirrors: the player may try the next one when this
        # URL never shows a picture. A row with none gets the single URL and is
        # not tracked -- the quality journal is about channels, and a YouTube
        # film or a torrent has nothing to fail over to.
        pipe.sources = list(sources) if sources else [url]
        pipe.source_index = pipe.sources.index(url) if url in pipe.sources else 0
        pipe.track = sources is not None
        return pipe

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
        if not files:
            files = self._torrent_subtitles(url, idx)
        source = LibtorrentProcess(url, idx, files or [])
        return self.build(title, source, subs, True)

    def _torrent_subtitles(self, url, idx):
        """
        Subtitle files inside the torrent that match the video being played.

        A direct play (e.g. from a channel) often carries no file-level subtitle
        mapping, even when the torrent contains .srt files. Discover them from
        the torrent's file list so they can be attached mid-play, the same way
        "View Files" does.
        """
        try:
            from ..api.torrent import SUBS_EXTS, VIDEO_EXTS, _subs_for, torrent_files

            tfiles = torrent_files(url)
            if not tfiles:
                return []
            subs_list = [
                (i, f[0])
                for i, f in enumerate(tfiles)
                if f[0].lower().endswith(SUBS_EXTS)
            ]
            if not subs_list:
                return []
            if idx == -1:
                target = None
                best = -1
                fallback = None
                fallback_size = -1
                for i, f in enumerate(tfiles):
                    name, size = f[0], f[1] if isinstance(f[1], int) else 0
                    if size > fallback_size:
                        fallback, fallback_size = i, size
                    if name.lower().endswith(VIDEO_EXTS) and size > best:
                        target, best = i, size
                if target is None:
                    target = fallback if fallback is not None else 0
            else:
                target = idx if 0 <= idx < len(tfiles) else 0
            name = tfiles[target][0] if 0 <= target < len(tfiles) else ""
            chosen = _subs_for(name, subs_list)
            return [{"idx": i, "name": n} for i, n in chosen]
        except Exception as exc:
            cherrypy.log("could not discover torrent subtitles: %s" % exc)
            return []

    def playLocalFile(self, filepath, title, playlist=None, index=None):
        self.playPipe(self.buildLocalFile(filepath, title), playlist, index)

    def buildLocalFile(self, filepath, title):
        # Local File path behaviour is like http
        return self.build(title, LocalFileProcess(filepath), http=True)

    def add_subtitles(self, chosen=None, lang=None):
        """
        Attach subtitles to the film that is playing, without starting it again.

        Raises rather than returning quietly: this is a button, and a button
        that does nothing is worse than one that says why.
        """
        pipe = self.play_pipe
        if pipe is None or not pipe.is_started():
            raise RuntimeError("Nothing is playing")
        if not pipe.add_subtitles(chosen, lang):
            raise RuntimeError("The player could not take those subtitles")
        return True

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
            # What the source is doing, when it can say: "12% · 1.4 MB/s · 6
            # peers". Shown while the film is not on screen; see status_msg.
            "Detail": "",
            # Whether the source said this is a live stream. The bar shows a
            # clock for a live stream because the player's position is the
            # rolling HLS window there, not where in anything the picture is --
            # it climbs past the window length it reports and never comes back.
            "Live": False,
            # Whether this film came from a playlist. The panel's prev/next item
            # buttons are about the list around the film, and a film played on
            # its own has no list for them to move through.
            "Playlist": self.playlist is not None,
        }

        if self.error is not None:
            status["Error"] = True
            status["Msg"] = self.error

        if self.notice is not None:
            status["Notice"] = self.notice

        if self.pending is not None and self.error is None:
            # A play has been asked for and the loop has not started it yet.
            # The usual reason is that the old pipe is still stopping: VLC
            # takes its time to let go of the display, and for those seconds
            # the bar used to go blank. Show the waiting instead.
            _pipe, started, from_playlist = self.pending
            status["State"] = ST_STARTING
            status["Title"] = "LOADING STREAM"
            status["Elapsed"] = max(0, int(time.time() - started))
            status["Playlist"] = from_playlist
            return status

        if play_pipe is None or self.error is not None:
            return status

        if self.play_requested_at is not None:
            status["Elapsed"] = int(time.time() - self.play_requested_at)

        status["Title"] = play_pipe.status_msg()
        status["Waiting"] = play_pipe.waiting()
        ask_live = getattr(play_pipe, "is_live", None)
        status["Live"] = bool(ask_live()) if callable(ask_live) else False
        detail = play_pipe.detail()
        if self._failover_note:
            detail = (
                self._failover_note + " · " + detail
                if detail
                else self._failover_note
            )
        status["Detail"] = detail
        # How long it took, once, for the question that keeps coming back:
        # "was that slower than it used to be". Everything a play waits on is
        # already logged with a timestamp -- the torrent source arming, yt-dlp
        # resolving, the player opening -- and this is the total.
        if (
            not self.start_logged
            and self.play_requested_at is not None
            and play_pipe.is_started()
            and not status["Waiting"]
        ):
            self.start_logged = True
            self._note_picture()
            cherrypy.log(
                "PLAY STARTED after %.1fs: %s"
                % (time.time() - self.play_requested_at, status["Title"])
            )
        if self._is_playing():
            status["State"] = ST_RUNNING
        elif not self._is_stopping():
            status["State"] = ST_STARTING
        if status["State"] == ST_RUNNING:
            self._add_playback(status, play_pipe)
            stalled = status.get("Stalled", 0)
            # A freeze is a picture that had started and then stopped. A
            # stream still waiting for its first frame is a slow start, and
            # the stage's own timeout is what ends that.
            picture = self._picture_after() is not None
            if stalled and picture and not self._stall_logged:
                self._stall_logged = True
                url = self._current_url()
                if url and getattr(play_pipe, "track", False):
                    playquality.record_froze(url)
            if (
                stalled >= _RECONNECT_AFTER
                and status.get("Live")
                and picture
                and getattr(play_pipe, "track", False)
                and not self._reconnect_sent
            ):
                self._reconnect_sent = True
                try:
                    self._enqueue(
                        MSG_PLAYER_RECONNECT, self._play_seq, self._attempt_seq
                    )
                except RuntimeError:
                    # A full queue means the loop is not answering; the next
                    # poll gets another chance rather than failing the status.
                    self._reconnect_sent = False
        status["Paused"] = self.paused
        return status

    def _add_playback(self, status, pipe):
        """
        Where the film is and whether it is still moving.

        The interface needs a clock that means something: the count it shows
        while loading is when the film was asked for, which says nothing once
        the film is running. The player's own position is the honest number --
        it is the same one a seek changes -- and it is read from the http
        interface rather than the cli, which is the one that stops answering
        when it is asked too often. See VlcProcess._status_snapshot.

        Stalled is the pairing of two clocks. For a film the picture clock is
        the player's own position: if it has not moved in fifteen seconds with
        the film neither paused nor followed, nothing is coming through and the
        screen is frozen. A live stream cannot use it -- VLC reports the rolling
        HLS window there, and answers with no position at all most of the time,
        so every live channel was reported frozen after fifteen seconds -- so
        it uses the input clock instead: no bytes arriving is the one reading
        that means the picture has stopped. The source clock is the swarm's --
        no peers and pieces still missing -- and it fires earlier, while the
        picture is still fine, because that is when there is still time to do
        something about it.
        """
        playback = pipe.playback() if hasattr(pipe, "playback") else {}
        self._sync_paused(playback.get("state"))
        position = playback.get("position")
        if position is not None:
            status["Position"] = int(position)
            length = playback.get("length")
            # VLC answers -1 for a stream whose length it does not know, which
            # is not a length: a bar reading "0:00 / -0:01" is worse than one
            # that leaves it out.
            if length and length > 0:
                status["Length"] = int(length)
            self._note_position(position)
        stalled, reason = self._stalled(pipe, playback)
        if stalled:
            status["Stalled"] = stalled
            status["StallReason"] = reason

    def _sync_paused(self, state):
        """
        Take the pause state from the player, not from the button.

        The interface flips its own idea of paused the moment the button is
        pressed, because that is what makes the button feel immediate. But the
        command travels through the cli, and the cli is the thing that stops
        answering -- measured on rpi, "pause -> no reply" with the film frozen.
        When that happened the bar went on showing a pause that may never have
        been applied, and because a paused film is never reported as stalled,
        the one warning that mattered was suppressed.

        So after the command has had a moment to land, the player's own state
        is the one that counts. Two seconds: long enough that the toggle is not
        immediately undone by a snapshot taken before the command arrived,
        short enough that a lie cannot outlive the action by much.
        """
        if state not in ("playing", "paused"):
            return
        if time.time() - self._paused_at < _PAUSE_SETTLE:
            return
        self.paused = state == "paused"

    def _note_position(self, position):
        now = time.time()
        if self._pos_last is None or int(position) != int(self._pos_last):
            self._pos_last = position
            self._pos_changed_at = now

    def _stalled(self, pipe, playback=None):
        """
        (seconds, reason) if something is stuck, else (0, "").

        A live stream is judged on the input, not the position: its position is
        the player's rolling window and often not reported at all, so using it
        as a clock said every live channel had frozen after fifteen seconds.
        Bytes still arriving means the stream is still being fed; none for
        fifteen seconds means the picture has stopped for everyone.
        """
        if self.paused:
            # A paused film looks exactly like a frozen one, and is not.
            return 0, ""
        ask = getattr(pipe, "is_live", None)
        if callable(ask) and ask():
            idle = (playback or {}).get("read_idle")
            if idle is None:
                # No fresh reading from the player: claiming a freeze would be
                # a guess, and the panel is the place for "cannot say".
                return 0, ""
            if idle >= _STALL_PICTURE_AFTER:
                return int(idle), "source"
            return 0, ""
        if self._pos_changed_at is not None:
            frozen = int(time.time() - self._pos_changed_at)
            if frozen >= _STALL_PICTURE_AFTER:
                return frozen, "picture"
        worst = 0
        for proc in getattr(pipe, "procs", []):
            ask = getattr(proc, "stalled_for", None)
            if callable(ask):
                try:
                    worst = max(worst, int(ask() or 0))
                except Exception:
                    continue
        if worst >= _STALL_SOURCE_AFTER:
            return worst, "source"
        return 0, ""

    def _picture_after(self):
        """Seconds from the play being asked for to the first frame, or None."""
        if self.play_requested_at is None:
            return None
        for stage in getattr(self.play_pipe, "procs", None) or []:
            media_at = getattr(stage, "_media_at", None)
            if media_at is not None:
                return round(media_at - self.play_requested_at, 1)
        return None

    def debug(self):
        """
        Everything that might explain what is happening, for the panel.

        Only what is cheap and local: two http interfaces that are already
        open, /proc, and the last lines each stage wrote. Nothing here waits on
        a network or on a player that is busy, because the panel is opened when
        something is already wrong.
        """
        status = self.status()
        pipe = self.play_pipe
        out = {
            "Playback": {
                "title": status["Title"],
                "state": status["State"],
                "paused": status["Paused"],
                "waiting": status["Waiting"],
                "elapsed": status["Elapsed"],
                "picture_after": self._picture_after(),
                "position": status.get("Position"),
                "length": status.get("Length"),
                "stalled": status.get("Stalled", 0),
                "stall_reason": status.get("StallReason", ""),
                "error": status["Msg"],
            },
            "Source": None,
            "Player": None,
            "Pipeline": [],
            "Machine": _machine(),
            "Log": {},
        }
        if pipe is None:
            return out
        try:
            pipe_debug = pipe.debug()
        except Exception as exc:
            pipe_debug = {"title": "", "stages": [], "error": str(exc)}
        for stage in pipe_debug.get("stages") or []:
            name = stage.get("name") or "stage"
            out["Pipeline"].append(
                {
                    "name": name,
                    "pid": stage.get("pid"),
                    "running": stage.get("running"),
                    "ready_after": stage.get("ready_after"),
                }
            )
            log = stage.get("log") or []
            if log:
                out["Log"][name] = log
            if stage.get("is_source"):
                out["Source"] = stage
            elif name == "vlc":
                out["Player"] = stage
        return out

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
                self._paused_at = time.time()


def _machine():
    """
    The readings that explain a display failure, from /proc.

    Video memory is here because it is the one number that has repeatedly
    explained a play that decodes and shows nothing: the decoder works, the
    picture is never committed, and the log says "Atomic commit failed: No
    space left on device" -- which is the display, not the disk. The rest is
    what a Pi running out of something looks like: RAM, cache, load, and room
    left for the buffer.
    """
    out = {}
    keys = {
        "CmaTotal": "cma_total_mb",
        "CmaFree": "cma_free_mb",
        "MemAvailable": "mem_available_mb",
        "Cached": "cached_mb",
    }
    try:
        with open("/proc/meminfo") as fh:
            mem = {}
            for line in fh:
                key, _, rest = line.partition(":")
                mem[key] = rest.strip()
    except OSError:
        mem = {}
    for key, name in keys.items():
        parts = mem.get(key, "").split()
        try:
            out[name] = int(parts[0]) // 1024
        except (ValueError, IndexError):
            continue
    total = out.get("cma_total_mb")
    free = out.get("cma_free_mb")
    if total and free is not None:
        out["cma_free_percent"] = int(free * 100 / total)
    try:
        out["load"] = "%.2f %.2f %.2f" % os.getloadavg()
    except OSError:
        pass
    try:
        usage = os.statvfs(DATA_PATH)
        out["disk_free_mb"] = usage.f_bavail * usage.f_frsize // (1024 * 1024)
    except OSError:
        pass
    return out


Player = _Player()

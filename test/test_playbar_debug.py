"""
The diagnostics the bar and its panel are built on.

Two kinds of reading, from two places that cannot answer the same way. The
source is asked over peerflix's own /.json, which is cheap and always there.
The player is asked over the lua http interface it already opens for subtitles
-- never over the cli, which is a single reply queue that stopped answering
when it was polled once a second, and which this feature must not bring back.

The stall warning is the pairing of the two clocks: the picture's position
(cannot move if the screen is frozen) and the swarm's peers (goes quiet before
the picture does, while there is still something to do about it).
"""

import os
import time

import pytest

import lib.player.pflixproc as pflix
import lib.player.player as player_module
import lib.player.vlcproc as vlcproc


class _Pipe:
    """A pipeline that only says where the film is and how it is stuck."""

    def __init__(self, playback=None, stalled=0):
        self._playback = playback or {}
        self._stalled = stalled
        self.procs = [self]

    def playback(self):
        return self._playback

    def stalled_for(self):
        return self._stalled

    def is_started(self):
        return True

    def is_stopping(self):
        return False

    def status_msg(self):
        return "A Film"

    def waiting(self):
        return False

    def detail(self):
        return ""


def _player(pipe, paused=False):
    pl = player_module._Player()
    pl.play_pipe = pipe
    pl.play_requested_at = time.time()
    pl.paused = paused
    return pl


class TestStallWarning:
    def test_a_moving_picture_is_never_stalled(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}))
        pl.status()
        assert "Stalled" not in pl.status()

    def test_a_frozen_picture_is_stalled_after_fifteen_seconds(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}))
        # The position was last seen moving twenty seconds ago.
        pl._pos_last = 10
        pl._pos_changed_at = time.time() - 20
        status = pl.status()
        assert status["Stalled"] >= 15
        assert status["StallReason"] == "picture"

    def test_a_freeze_under_the_threshold_is_quiet(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}))
        pl._pos_last = 10
        pl._pos_changed_at = time.time() - 5
        assert "Stalled" not in pl.status()

    def test_paused_is_never_stalled(self):
        """A paused film looks exactly like a frozen one, and is not."""
        pl = _player(_Pipe(playback={"position": 10, "length": 100}), paused=True)
        pl._pos_last = 10
        pl._pos_changed_at = time.time() - 60
        assert "Stalled" not in pl.status()

    def test_the_source_can_warn_before_the_picture_stops(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}, stalled=45))
        status = pl.status()
        assert status["Stalled"] >= 30
        assert status["StallReason"] == "source"

    def test_a_quiet_source_under_the_threshold_is_quiet(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}, stalled=12))
        assert "Stalled" not in pl.status()

    def test_picture_wins_over_source(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}, stalled=45))
        pl._pos_last = 10
        pl._pos_changed_at = time.time() - 20
        assert pl.status()["StallReason"] == "picture"


class TestPlaybackFields:
    def test_position_and_length_come_from_the_player(self):
        pl = _player(_Pipe(playback={"position": 61.4, "length": 3000}))
        status = pl.status()
        assert status["Position"] == 61
        assert status["Length"] == 3000

    def test_no_position_when_the_player_will_not_say(self):
        pl = _player(_Pipe(playback={}))
        assert "Position" not in pl.status()

    def test_a_pipe_without_playback_is_not_an_error(self):
        pl = _player(_Pipe())
        assert pl.status()["State"] == player_module.ST_RUNNING


class TestVlcStatusSnapshot:
    def _proc(self, monkeypatch, payload, count):
        proc = vlcproc.VlcProcess()
        proc._http_port = 1234
        proc._http_password = "secret"

        def fake_get(name, timeout=2):
            count.append(name)
            return payload

        monkeypatch.setattr(proc, "_http_get", fake_get)
        return proc

    def test_the_snapshot_is_normalised(self, monkeypatch):
        calls = []
        payload = {
            "state": "playing",
            "time": "61",
            "length": "3000",
            "stats": {"decodedvideo": "900", "displayedpictures": "890"},
            "information": {
                "category": {
                    "Stream 0": {"Codec": "H264", "Type": "Video", "Video resolution": "1920x1080"}
                }
            },
        }
        proc = self._proc(monkeypatch, payload, calls)
        snapshot = proc.playback()
        assert snapshot["position"] == 61.0
        assert snapshot["length"] == 3000.0
        assert snapshot["stats"]["decodedvideo"] == 900.0
        assert snapshot["streams"][0]["Video resolution"] == "1920x1080"

    def test_repeated_reads_within_a_second_ask_once(self, monkeypatch):
        """
        Every open tab asks the server for the status once a second; the cache
        is what keeps that from becoming one request per tab to a player that
        is busy decoding.
        """
        calls = []
        proc = self._proc(monkeypatch, {"state": "playing", "time": "5"}, calls)
        proc.playback()
        proc.playback()
        proc.playback()
        assert len(calls) == 1

    def test_the_snapshot_expires(self, monkeypatch):
        calls = []
        proc = self._proc(monkeypatch, {"state": "playing", "time": "5"}, calls)
        proc.playback()
        proc._snapshot = (time.time() - 5, proc._snapshot[1])
        proc.playback()
        assert len(calls) == 2

    def test_an_unreachable_player_is_empty_not_fatal(self, monkeypatch):
        proc = vlcproc.VlcProcess()
        proc._http_port = None
        assert proc.playback() == {}
        assert proc._http_get("status.json") is None


class TestNormalizeStatus:
    def test_missing_information_is_survivable(self):
        snapshot = vlcproc._normalize_status({"state": "playing"})
        assert snapshot["streams"] == []
        assert snapshot["position"] is None

    def test_category_as_a_list(self):
        """The lua interface emits either a table or a list, depending."""
        payload = {
            "information": {
                "category": [
                    {"name": "Stream 0", "info": {"Type": "Audio", "Codec": "A52"}}
                ]
            }
        }
        snapshot = vlcproc._normalize_status(payload)
        assert snapshot["streams"] == [{"Type": "Audio", "Codec": "A52"}]


class TestTrackListing:
    def _proc(self, monkeypatch, replies):
        proc = vlcproc.VlcProcess()
        proc._http_port = 1234
        proc._http_password = "secret"

        def fake_collect(command):
            return replies.get(command, [])

        monkeypatch.setattr(proc, "_send_and_collect", fake_collect)
        return proc

    def test_the_current_track_is_marked(self, monkeypatch):
        proc = self._proc(
            monkeypatch,
            {
                "vtrack": ["+----[ Stream 0 ]", "|  1 - Video - [English] *", "+----"],
                "atrack": ["|  2 - English - [English]", "|  3 - Japanese - [Japanese] *"],
                "strack": ["|  4 - SDH - [English]", "| -1 - Disable"],
            },
        )
        tracks = proc.debug()["tracks"]
        assert [(t["kind"], t["id"], t["current"]) for t in tracks] == [
            ("video", 1, True),
            ("audio", 2, False),
            ("audio", 3, True),
            ("subtitle", 4, False),
        ]

    def test_disable_is_not_a_track(self, monkeypatch):
        proc = self._proc(monkeypatch, {"strack": ["| -1 - Disable"]})
        assert proc.debug()["tracks"] == []

    def test_the_listing_is_kept_for_half_a_minute(self, monkeypatch):
        """Three cli commands a second is exactly what this feature avoids."""
        calls = []
        proc = self._proc(monkeypatch, {"atrack": ["|  1 - A - [English]"]})
        proc._send_and_collect = lambda command: (calls.append(command), ["|  1 - A - [English]"])[1]
        proc.debug()
        proc.debug()
        proc.debug()
        assert calls.count("atrack") == 1

    def test_a_player_that_will_not_answer_is_empty(self, monkeypatch):
        proc = vlcproc.VlcProcess()
        proc._http_port = None
        assert proc.debug()["tracks"] == []


class TestPeerflixDisk:
    def _proc(self, tmp_path, monkeypatch, idx=0):
        monkeypatch.setattr(pflix, "BUFFER_DIR", str(tmp_path))
        proc = pflix.PeerflixProcess("magnet:?xt=urn:btih:abc", idx)
        proc._started_at = time.time()
        return proc

    def test_the_share_on_disk_is_allocated_bytes(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, monkeypatch)
        path = tmp_path / "film.mkv"
        # A sparse file: 10 MB logical, 1 MB actually written.
        with open(path, "wb") as fh:
            fh.truncate(10 * 1024 * 1024)
            fh.seek(0)
            fh.write(b"x" * (1024 * 1024))
            fh.flush()
            os.fsync(fh.fileno())
        proc._progress = {"files": [{"name": "film.mkv", "length": 10 * 1024 * 1024}]}
        info = proc.served_file()
        assert info["name"] == "film.mkv"
        assert info["percent"] == 10
        assert info["present"] < info["length"]

    def test_a_complete_file_reads_one_hundred_percent(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, monkeypatch)
        path = tmp_path / "film.mkv"
        path.write_bytes(b"x" * (1024 * 1024))
        proc._progress = {"files": [{"name": "film.mkv", "length": path.stat().st_size}]}
        assert proc.served_file()["percent"] == 100

    def test_a_file_in_a_torrent_folder_is_found(self, tmp_path, monkeypatch):
        """peerflix names files relative to the torrent, which is a folder."""
        proc = self._proc(tmp_path, monkeypatch)
        folder = tmp_path / "A.Pack"
        folder.mkdir()
        (folder / "E01.mkv").write_bytes(b"x" * 1024)
        proc._progress = {"files": [{"name": "E01.mkv", "length": 1024}]}
        assert proc.served_file()["present"] > 0

    def _set_swarm(self, proc, peers, downloaded=0):
        proc._progress = {
            "peers": peers,
            "downloaded": downloaded,
            "percent": 0,
            "speed": 0,
            "files": [{"name": "film.mkv", "length": 100}],
        }

    def test_a_complete_film_with_no_peers_is_not_stalled(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, monkeypatch)
        (tmp_path / "film.mkv").write_bytes(b"x" * 100)
        self._set_swarm(proc, peers=0)
        proc._peers_seen_at = time.time() - 600
        assert proc.stalled_for() == 0

    def test_peers_mean_it_is_working(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, monkeypatch)
        (tmp_path / "film.mkv").write_bytes(b"x" * 10)
        self._set_swarm(proc, peers=3)
        proc._peers_seen_at = time.time() - 600
        assert proc.stalled_for() == 0

    def test_missing_pieces_and_no_peers_is_stalled(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, monkeypatch)
        (tmp_path / "film.mkv").write_bytes(b"x" * 10)
        self._set_swarm(proc, peers=0)
        proc._peers_seen_at = time.time() - 90
        assert proc.stalled_for() >= 89

    def test_without_an_answer_from_the_swarm_nothing_is_claimed(self, tmp_path, monkeypatch):
        proc = self._proc(tmp_path, monkeypatch)
        assert proc.served_file() is None
        assert proc.stalled_for() == 0


class TestPlayerDebug:
    def test_the_panel_gets_every_section(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}))
        pl.play_pipe = _FakePipe()
        debug = pl.debug()
        assert debug["Playback"]["title"] == "A Film"
        assert debug["Source"]["name"] == "peerflix"
        assert debug["Player"]["name"] == "vlc"
        assert [s["name"] for s in debug["Pipeline"]] == ["peerflix", "vlc"]
        assert "cma_free_mb" in debug["Machine"]
        assert debug["Log"]["vlc"] == ["a line"]

    def test_without_a_pipe_it_still_answers(self):
        pl = player_module._Player()
        debug = pl.debug()
        assert debug["Playback"]["state"] == player_module.ST_NOT_RUNNING
        assert debug["Source"] is None


class TestSubtitleWait:
    def _proc(self, monkeypatch, replies, sleeps):
        proc = vlcproc.VlcProcess()
        proc._http_port = 1234
        proc._http_password = "secret"
        proc.proc = type("P", (), {"poll": lambda self: None})()
        calls = []

        def fake_ids(command):
            calls.append(command)
            return replies.pop(0) if replies else []

        monkeypatch.setattr(proc, "_track_ids", fake_ids)
        monkeypatch.setattr(vlcproc, "time", type("T", (), {
            "time": staticmethod(lambda: 0),
            "sleep": staticmethod(lambda seconds: sleeps.append(seconds)),
        }))
        return proc, calls

    def test_a_film_without_subtitles_asks_a_handful_of_times(self, monkeypatch):
        """
        This used to be sixty asks, one a second: sixty cli commands in the
        first minute of every YouTube video, and the cli stops answering when
        it is asked too often -- measured, "pause -> no reply".
        """
        sleeps = []
        proc, calls = self._proc(monkeypatch, [], sleeps)
        proc._wait_for_subtitles({})
        assert len(calls) == 4
        assert sleeps == [1.0, 2.0, 4.0]

    def test_the_first_listing_that_has_a_track_is_used(self, monkeypatch):
        sleeps = []
        proc, calls = self._proc(monkeypatch, [[], [], [7]], sleeps)
        sent = []
        monkeypatch.setattr(proc, "_send_command", lambda c: sent.append(c))
        proc._wait_for_subtitles({})
        assert sent == ["strack 7"]
        assert len(calls) == 3

    def test_a_user_action_ends_the_wait(self, monkeypatch):
        sleeps = []
        proc, calls = self._proc(monkeypatch, [], sleeps)
        proc._acted = True
        proc._wait_for_subtitles({})
        assert calls == []


class TestPauseTruth:
    def test_the_players_state_wins_after_the_command_settles(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100, "state": "paused"}))
        pl.paused = False
        pl._paused_at = time.time() - 5
        assert pl.status()["Paused"] is True

    def test_the_toggle_is_trusted_immediately(self):
        """The button must not flip back under the hand that pressed it."""
        pl = _player(_Pipe(playback={"position": 10, "length": 100, "state": "playing"}))
        pl.paused = True
        pl._paused_at = time.time()
        assert pl.status()["Paused"] is True

    def test_a_state_the_player_will_not_name_is_ignored(self):
        pl = _player(_Pipe(playback={"position": 10, "length": 100}))
        pl.paused = True
        pl._paused_at = time.time() - 5
        assert pl.status()["Paused"] is True


class TestNoReply:
    def test_a_command_with_no_answer_is_said_and_remembered(self, monkeypatch, capsys):
        proc = vlcproc.VlcProcess()
        monkeypatch.setattr(proc, "_send_and_collect", lambda command: None)
        assert proc._send_command("pause") is False
        assert proc.debug()["last_command"] == {"command": "pause", "replied": False}

    def test_an_answered_command_is_remembered_too(self, monkeypatch):
        proc = vlcproc.VlcProcess()
        monkeypatch.setattr(proc, "_send_and_collect", lambda command: ["ok"])
        assert proc._send_command("seek 10") is True
        assert proc.debug()["last_command"] == {"command": "seek 10", "replied": True}


class TestInputIdle:
    def _proc(self, monkeypatch, readings):
        proc = vlcproc.VlcProcess()
        proc._http_port = 1234
        proc._http_password = "secret"
        values = list(readings)

        def fake_get(name, timeout=2):
            return {"state": "playing", "time": 1, "stats": {"readbytes": values.pop(0)}}

        monkeypatch.setattr(proc, "_http_get", fake_get)
        return proc

    def test_bytes_arriving_keep_the_idle_clock_at_zero(self, monkeypatch):
        proc = self._proc(monkeypatch, [1000, 5000, 9000])
        now = [100.0]

        monkeypatch.setattr(vlcproc, "time", type("T", (), {
            "time": staticmethod(lambda: now[0]),
        }))
        first = proc.playback()          # first sample has no rate yet
        assert "read_speed" not in first
        now[0] += 1
        second = proc.playback()
        assert second["read_speed"] == 4000
        assert second["read_idle"] == 0

    def test_a_dead_input_shows_nothing_arriving(self, monkeypatch):
        proc = self._proc(monkeypatch, [1000, 5000, 5000, 5000])
        now = [100.0]
        monkeypatch.setattr(vlcproc, "time", type("T", (), {
            "time": staticmethod(lambda: now[0]),
        }))
        proc.playback()
        now[0] += 1
        proc.playback()
        now[0] += 1
        proc._snapshot = None
        third = proc.playback()
        now[0] += 1
        proc._snapshot = None
        fourth = proc.playback()
        assert fourth["read_speed"] == 0
        assert fourth["read_idle"] >= 2


class TestDropFileCache:
    def test_one_file_is_evicted(self, tmp_path):
        path = tmp_path / "film.mkv"
        path.write_bytes(b"x" * 4096)
        assert pflix.drop_file_cache(str(path)) == 1

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert pflix.drop_file_cache(str(tmp_path / "absent.mkv")) == 0

    def test_no_path_drops_nothing(self):
        assert pflix.drop_file_cache(None) == 0
        assert pflix.drop_file_cache("") == 0


class TestLocalFileEviction:
    def test_only_a_local_file_gets_evicted(self, tmp_path, monkeypatch):
        """
        A stream never touches this filesystem, and a torrent's buffer is the
        source stage's job. Evicting what is not being read would be a syscall
        per play for nothing.
        """
        calls = []
        monkeypatch.setattr(vlcproc, "drop_file_cache", lambda p: calls.append(p))
        monkeypatch.setattr(vlcproc, "_EVICT_EVERY", 0)

        local = tmp_path / "film.mkv"
        local.write_bytes(b"x")
        proc = vlcproc.VlcProcess()
        proc._input_url = str(local)
        proc.killing = True
        proc._evict_input()
        assert calls == []

        proc = vlcproc.VlcProcess()
        proc._input_url = "http://192.168.1.119:9696"
        proc.killing = True
        proc._evict_input()
        assert calls == []

    def test_it_drops_the_file_while_it_runs(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(vlcproc, "drop_file_cache", lambda p: calls.append(p))
        monkeypatch.setattr(vlcproc, "_EVICT_EVERY", 0)
        local = tmp_path / "film.mkv"
        local.write_bytes(b"x")

        proc = vlcproc.VlcProcess()
        proc._input_url = str(local)
        proc.killing = False

        def stop_after_one(seconds):
            # After the first eviction, the next sleep's tick stops the loop.
            if calls:
                proc.killing = True

        monkeypatch.setattr(vlcproc.time, "sleep", stop_after_one)
        proc._evict_input()
        assert calls == [str(local)]


class TestDisplayWatchdog:
    def _proc(self, monkeypatch, snapshots):
        proc = vlcproc.VlcProcess()
        values = list(snapshots)
        proc.proc = type("P", (), {"poll": lambda self: None})()
        monkeypatch.setattr(proc, "_status_snapshot", lambda: values.pop(0) if values else {})
        quit_calls = []
        monkeypatch.setattr(proc, "_force_quit", lambda: quit_calls.append(1))
        monkeypatch.setattr(vlcproc.time, "sleep", lambda seconds: None)
        # A clock that advances a second per reading, so the loop's deadline is
        # reached without sleeping.
        state = {"now": 0.0}

        def tick():
            state["now"] += 1.0
            return state["now"]

        monkeypatch.setattr(vlcproc.time, "time", tick)
        return proc, quit_calls

    def test_a_decoded_film_with_no_picture_ends_the_play(self, monkeypatch):
        """
        The screen is black, the demuxer is open, frames are decoded -- so the
        bar stops waiting -- and nothing says why. This is the one failure the
        viewer cannot tell from a slow stream, so it is ended with the reason.
        """
        proc, quits = self._proc(
            monkeypatch,
            [{"stats": {"decodedvideo": 5, "displayedpictures": 0}}] * 5,
        )
        proc._watch_display()
        assert quits == [1]
        assert proc.has_error()

    def test_a_displayed_frame_ends_the_watch(self, monkeypatch):
        proc, quits = self._proc(
            monkeypatch,
            [{"stats": {"decodedvideo": 5, "displayedpictures": 3}}],
        )
        proc._watch_display()
        assert quits == []
        assert not proc.has_error()

    def test_nothing_decoded_yet_is_not_a_display_failure(self, monkeypatch):
        """A torrent with no peers decodes nothing; that is not this."""
        proc, quits = self._proc(
            monkeypatch,
            [{"stats": {"decodedvideo": 0, "displayedpictures": 0}}] * 4,
        )
        proc._watch_display()
        assert quits == []
        assert not proc.has_error()


class TestSourceModeset:
    def test_the_display_follows_the_film_by_default(self):
        cmd = vlcproc.VlcProcess().build_command({"outfile": "/x"})
        assert "--drm-vout-source-modeset" in cmd

    def test_it_can_be_turned_off(self):
        cmd = vlcproc.VlcProcess({"source_modeset": "0"}).build_command({"outfile": "/x"})
        assert "--drm-vout-source-modeset" not in cmd


class _Stage:
    def __init__(self, name, **extra):
        self._name = name
        self._extra = extra

    def name(self):
        return self._name

    def debug(self):
        base = {
            "name": self._name,
            "pid": 123,
            "running": True,
            "ready_after": 1.5,
            "log": ["a line"] if self._name == "vlc" else [],
        }
        base.update(self._extra)
        return base


class _FakePipe(_Pipe):
    def __init__(self):
        super().__init__(playback={"position": 10, "length": 100})
        self.procs = [_Stage("peerflix"), _Stage("vlc")]

    def debug(self):
        return {
            "title": "A Film",
            "stages": [p.debug() for p in self.procs],
        }

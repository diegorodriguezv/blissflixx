"""
lib/player/ltproc.py

The libtorrent source hands the player an HTTP URL and reports the swarm's
numbers the playbar reads
them through detail() and the diagnostics panel through debug().

Everything here runs without libtorrent or a swarm: the command is built, and
the lines bt_serve.py would print are fed through the same seam every other
stage is tested through. What that leaves untested is the transfer itself,
which is measured on the box.
"""

import json
import time

import pytest

from lib.player import ltproc
from lib.player import player as player_module
from lib.player.ltproc import BT_SERVE, SYSTEM_PYTHON, LibtorrentProcess
from lib.player.processpipe import ProcessException
from lib.player.buffer import BUFFER_DIR

MAGNET = "magnet:?xt=urn:btih:" + "A" * 40
SERVING = (
    "bf-bt: serving http://127.0.0.1:9697/ index=0 "
    "name=Film.mkv length=1880064000 pieces=1025 top=Film.mkv"
)

STATUS = {
    "state": "downloading",
    "peers": 12,
    "seeds": 3,
    "speed": 1500000,
    "payload": 220000000,
    "pieces": [120, 1025],
    "files": [
        {
            "index": 0,
            "name": "Film.mkv",
            "length": 1000,
            "present": 340,
            "percent": 34,
        }
    ],
    "playing": 0,
    "clamped": False,
    "window": [120, 127],
    "tail": False,
    "uptime": 30,
}


def feed(proc, *lines):
    """Make proc._readline() yield the given lines, then run dry."""
    remaining = iter(lines)

    def _readline(timeout=None):
        try:
            return next(remaining)
        except StopIteration:
            raise ProcessException("no more output")

    proc._readline = _readline
    return proc


def with_status(proc, **fields):
    stats = dict(STATUS)
    stats.update(fields)
    # The buffer entry comes from the serving line; a test that only feeds
    # status lines still has to name the entry the space check must not delete.
    if proc._top is None:
        proc._top = "Film.mkv"
    proc._watch_status("bf-status: " + json.dumps(stats))
    return proc


class TestCommand:
    def test_runs_the_torrent_process_under_the_system_python(self):
        """
        libtorrent comes from python3-libtorrent, which is installed into the
        system interpreter; the application runs in a virtualenv that cannot see
        it. Getting this wrong is an ImportError at play time.
        """
        proc = LibtorrentProcess(MAGNET, 0)
        assert proc.cmd[0] == SYSTEM_PYTHON
        assert proc.cmd[1] == BT_SERVE

    def test_what_it_plays_is_said_in_the_arguments(self):
        proc = LibtorrentProcess(MAGNET, 2)
        cmd = proc.cmd
        assert cmd[cmd.index("--dir") + 1] == BUFFER_DIR
        assert cmd[cmd.index("--port") + 1] == "9697"
        assert cmd[cmd.index("--index") + 1] == "2"

    def test_the_torrent_keeps_its_own_trackers_and_gains_ours(self):
        """
        A magnet from a channel often names only trackers that have been dead
        for years, and the swarm is found through the ones we add.
        """
        proc = LibtorrentProcess(MAGNET + "&tr=udp://mine.example:80/announce", 0)
        magnet = proc.cmd[2]
        assert "mine.example" in magnet
        assert "tracker.opentrackr.org" in magnet

    def test_subtitle_files_are_named_to_the_torrent_process(self):
        proc = LibtorrentProcess(
            MAGNET, 1, [{"idx": 3, "name": "Film.eng.srt"}, {"idx": 4, "name": "Film.spa.srt"}]
        )
        assert proc.cmd[proc.cmd.index("--subs") + 1] == "3,4"

    def test_no_subtitles_means_no_argument(self):
        proc = LibtorrentProcess(MAGNET, 1)
        assert "--subs" not in proc.cmd


class TestReady:
    def test_the_serving_line_is_the_output_url(self):
        proc = feed(LibtorrentProcess(MAGNET, 0), SERVING)
        proc.args = {}
        assert proc._ready()["outfile"] == "http://127.0.0.1:9697"

    def test_no_subtitles_wanted_keeps_the_shape_the_player_expects(self):
        proc = feed(LibtorrentProcess(MAGNET, 0), SERVING)
        proc.args = {}
        assert "subtitles_wanted" not in proc._ready()

    def test_subtitles_are_urls_inside_the_torrent_process(self):
        """
        The player fetches a subtitle in the background and adds it mid-play, so
        this stage hands over the address, never the bytes: waiting for a
        subtitle's pieces here would delay the film.
        """
        proc = feed(
            LibtorrentProcess(MAGNET, 0, [{"idx": 3, "name": "Film.eng.srt"}]),
            SERVING,
        )
        proc.args = {}
        wanted = proc._ready()["subtitles_wanted"]
        assert wanted == [
            {"url": "http://127.0.0.1:9697/3", "name": "Film.eng.srt"}
        ]

    def test_a_process_that_dies_carries_the_command_too(self):
        """
        A crash has no message of its own. The exit status plus the command is
        all there is to reproduce it with.
        """
        proc = feed(LibtorrentProcess(MAGNET, 0))
        proc.args = {}
        with pytest.raises(ProcessException) as raised:
            proc._ready()
        message = str(raised.value)
        assert "no more output" in message
        assert " ".join(proc.cmd) in message

    def test_a_fatal_line_fails_the_stage_and_carries_the_command(self):
        """
        The application's rule for a failure the person has to act on: say what
        went wrong, and give the command that reproduces it.
        """
        proc = feed(
            LibtorrentProcess(MAGNET, 0),
            "bf-bt: error libtorrent is not installed for /usr/bin/python3; "
            "run: sudo apt-get install python3-libtorrent",
        )
        proc.args = {}
        with pytest.raises(ProcessException) as raised:
            proc._ready()
        message = str(raised.value)
        assert "python3-libtorrent" in message
        assert " ".join(proc.cmd) in message


class TestDetail:
    def test_the_number_is_the_share_of_the_file_on_disk(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0))
        assert proc.detail() == "34% on disk · 1.4 MB/s · 12 peers"

    def test_while_metadata_is_missing_it_says_who_has_answered(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0), state="metadata", peers=41)
        assert proc.detail() == "41 peers"

    def test_one_peer_is_not_plural(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0), state="metadata", peers=1)
        assert proc.detail() == "1 peer"

    def test_a_silent_swarm_says_how_long_it_has_been_silent(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0), peers=0)
        proc._started_at = time.time() - 40
        assert "nothing arriving for 40s" in proc.detail()

    def test_a_film_complete_on_disk_is_not_a_stall(self):
        """
        Pieces already here are never downloaded again, so a finished film has
        no peers and no traffic. Reported as stuck, that reads as a fault when
        it is the best possible state.
        """
        proc = with_status(
            LibtorrentProcess(MAGNET, 0),
            peers=0,
            files=[
                {
                    "index": 0,
                    "name": "Film.mkv",
                    "length": 1000,
                    "present": 1000,
                    "percent": 100,
                }
            ],
        )
        proc._started_at = time.time() - 400
        assert proc.stalled_for() == 0
        assert "nothing arriving" not in proc.detail()

    def test_a_hash_check_is_not_a_stall(self):
        """
        Checking the card runs with no peers and might take a minute on a 2GB
        film; reported as a stall it would read as a dead torrent.
        """
        proc = with_status(
            LibtorrentProcess(MAGNET, 0), state="checking_files", checking=20, peers=0
        )
        proc._started_at = time.time() - 90
        assert proc.stalled_for() == 0

    def test_peers_present_is_not_a_stall_even_if_slow(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0), peers=1)
        proc._started_at = time.time() - 400
        assert proc.stalled_for() == 0

    def test_nothing_to_say_before_the_first_status(self):
        assert LibtorrentProcess(MAGNET, 0).detail() == ""


class TestServedFile:
    def test_the_file_being_played_is_measured(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0))
        assert proc.served_file() == {
            "name": "Film.mkv",
            "length": 1000,
            "present": 340,
            "percent": 34,
        }

    def test_an_index_past_the_end_falls_back_to_the_first_file(self):
        """
        A saved play of a pack whose file list changed. The torrent process
        already fell back to file 0; the bar must measure the same file.
        """
        proc = with_status(
            LibtorrentProcess(MAGNET, 9),
            playing=0,
            files=[
                {"index": 0, "name": "One.mkv", "length": 100, "present": 50, "percent": 50}
            ],
        )
        assert proc.served_file()["name"] == "One.mkv"

    def test_an_empty_file_list_says_nothing(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0), files=[])
        assert proc.served_file() is None


class TestStatusLine:
    def test_the_state_names_the_wait(self):
        proc = LibtorrentProcess(MAGNET, 0)
        assert proc.status_msg() == "LOADING STREAM"
        with_status(proc, state="metadata")
        assert proc.status_msg() == "LOOKING FOR PEERS"

    def test_a_broken_status_line_is_ignored_rather_than_raising(self):
        """A stage must not fail a play over a line it could not read."""
        proc = LibtorrentProcess(MAGNET, 0)
        proc._watch_status("bf-status: not json")
        assert proc._status is None

    def test_other_lines_are_ignored(self):
        proc = LibtorrentProcess(MAGNET, 0)
        proc._watch_status("bb-bt: something else")
        assert proc._status is None


class TestDebug:
    def test_the_panel_gets_the_swarm_and_the_command(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0))
        debug = proc.debug()
        assert debug["swarm"]["peers"] == 12
        assert debug["swarm"]["window"] == [120, 127]
        assert debug["file"]["percent"] == 34
        assert debug["buffer"]["dir"] == BUFFER_DIR
        assert "needed" in debug["buffer"]
        assert "free" in debug["buffer"]
        assert "bt_serve.py" in debug["command"]


class TestBufferSpace:
    """
    The same rule the space manager applies to any torrent play: free
    what the film still needs plus headroom, by deleting whole buffer entries,
    least recently used first, and never the one being played.
    """

    def run_ready(self, proc):
        proc.args = {}
        return proc._ready()

    def test_the_decision_is_made_once_for_the_play(self, monkeypatch):
        calls = []

        def fake_reclaim(root, needed, current=None, free=None):
            calls.append((root, needed, current))
            return 0, [], 10 ** 12

        monkeypatch.setattr(ltproc.buffer_space, "reclaim", fake_reclaim)
        proc = feed(LibtorrentProcess(MAGNET, 0), SERVING)
        self.run_ready(proc)
        with_status(proc)
        with_status(proc, downloaded=999999)
        assert len(calls) == 1
        assert calls[0][0] == BUFFER_DIR
        # the bytes still to come, plus the headroom the app leaves
        assert calls[0][1] == (1000 - 340) + ltproc.buffer_space.HEADROOM

    def test_the_entry_being_played_is_named_so_it_is_never_deleted(self, monkeypatch):
        seen = []
        monkeypatch.setattr(
            ltproc.buffer_space,
            "reclaim",
            lambda root, needed, current=None, free=None: (
                seen.append(current) or (0, [], 10 ** 12)
            ),
        )
        proc = feed(LibtorrentProcess(MAGNET, 0), SERVING)
        self.run_ready(proc)
        with_status(proc)
        assert seen == ["Film.mkv"]

    def test_no_decision_before_the_size_is_known(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            ltproc.buffer_space,
            "reclaim",
            lambda *a, **k: calls.append(1) or (0, [], 10 ** 12),
        )
        proc = with_status(LibtorrentProcess(MAGNET, 0), files=[])
        assert calls == []
        # and it tries again on the next line that does have the size
        with_status(proc)
        assert calls == [1]

    def test_a_failure_to_free_space_does_not_break_the_play(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("no such file")

        monkeypatch.setattr(ltproc.buffer_space, "reclaim", boom)
        proc = with_status(LibtorrentProcess(MAGNET, 0))
        assert proc._space_checked is True
        assert proc.stalled_for() == 0


class TestPanelTimes:
    def test_the_panel_is_told_how_long_the_stage_took_to_serve(self):
        proc = LibtorrentProcess(MAGNET, 0)
        proc._ready_after = 4.2
        assert proc.debug()["serve_after"] == 4.2

    def test_a_hash_check_is_reported_as_verifying(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0), state="checking_files", checking=37)
        assert proc.debug()["verifying"] == 37

    def test_no_check_means_the_verify_line_says_nothing(self):
        proc = with_status(LibtorrentProcess(MAGNET, 0))
        assert proc.debug()["verifying"] is None

"""
Command construction for every pipeline stage.

Each stage's _get_cmd() builds the command it will run and does no I/O, so the
whole command surface is testable without omxplayer, peerflix, dlsrv or any
Raspberry Pi. Only the shape of each command is asserted: these tests check
that the right flags, paths and arguments are assembled, not that any binary is
installed. Whether omxplayer actually renders video is a question for the
hardware, not for this suite.

Every backend now exposes build_command(args) directly, which is pure and
depends only on args, so no command here needs a process, a shell or the binary
being installed.
"""

import os

import pytest

from lib.locations import BIN_PATH
from lib.player.dlsrvproc import DlsrvProcess
from lib.player.localproc import LocalFileProcess
from lib.player.omxproc import OMX_CMD, OmxplayerProcess
from lib.player.omxproc2 import _CMD_FIFO, OmxplayerProcess2
from lib.player.pflixproc import PeerflixProcess
from lib.player.subsproc import SubtitlesProcess
from lib.player.ytdlproc import YoutubeDlProcess

# ythelper.YOUTUBE_URL requires an exactly 11-character video id, so a short
# placeholder would silently fall through to "best" and hide the assertion.
YT_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SHORT_URL = "https://youtu.be/dQw4w9WgXcQ"
# BBC_URL likewise requires an 8-character programme id.
BBC_URL_SAMPLE = "https://www.bbc.co.uk/iplayer/episode/b0000001"
HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"
FILE_OUT = "/tmp/blissflixx/bf.out"
SUBS = "/tmp/blissflixx/movie.srt"


class TestDlsrv:
    def test_argv_is_binary_then_outfile(self):
        cmd = DlsrvProcess()._get_cmd({"outfile": FILE_OUT})
        assert cmd[0].endswith("dlsrv")
        assert cmd[1] == FILE_OUT

    def test_pid_appended_when_present(self):
        cmd = DlsrvProcess()._get_cmd({"outfile": FILE_OUT, "pid": 4242})
        assert cmd == [cmd[0], FILE_OUT, "4242"]

    def test_no_pid_means_two_arguments(self):
        cmd = DlsrvProcess()._get_cmd({"outfile": FILE_OUT})
        assert len(cmd) == 2

    def test_pid_is_stringified(self):
        """argv must be all strings; Popen accepts ints too but not mixed safely."""
        cmd = DlsrvProcess()._get_cmd({"outfile": FILE_OUT, "pid": 7})
        assert all(isinstance(a, str) for a in cmd)

    def test_uses_argv_not_shell_string(self):
        """dlsrv has no pipes or redirects, so it must not go through a shell."""
        assert isinstance(DlsrvProcess()._get_cmd({"outfile": FILE_OUT}), list)


class TestOmxplayer2:
    """Pipes the command FIFO into omxplayer so key presses are read as stdin."""

    def test_tails_the_command_fifo(self):
        cmd = OmxplayerProcess2()._get_cmd({"outfile": FILE_OUT})
        assert cmd.startswith("tail -f " + _CMD_FIFO + " | ")

    def test_outfile_is_single_quoted(self):
        cmd = OmxplayerProcess2()._get_cmd({"outfile": FILE_OUT})
        assert cmd.endswith("'" + FILE_OUT + "'")

    def test_subtitles_injected_when_present(self):
        cmd = OmxplayerProcess2()._get_cmd({"outfile": FILE_OUT, "subtitles": SUBS})
        assert "--align center --subtitles '" + SUBS + "'" in cmd

    def test_no_subtitles_flag_when_absent(self):
        cmd = OmxplayerProcess2()._get_cmd({"outfile": FILE_OUT})
        assert "--subtitles" not in cmd

    def test_no_pid_needed(self):
        """
        Unlike omxproc, this stage does not tail the producer, so it takes
        outfile alone. That difference is why a file:// url can reach it.
        """
        cmd = OmxplayerProcess2()._get_cmd({"outfile": FILE_OUT})
        assert "--pid" not in cmd


class TestOmxplayer:
    def test_http_outfile_is_quoted_directly(self):
        cmd = OmxplayerProcess().build_command({"outfile": HTTP_OUT})
        assert cmd == OMX_CMD + "'" + HTTP_OUT + "'"

    def test_no_tail_for_http(self):
        """An http url is streamed, so there is nothing to wait for on disk."""
        assert "tail" not in OmxplayerProcess().build_command({"outfile": HTTP_OUT})

    def test_local_file_tails_the_producer(self):
        """
        A local file is still being written by the download stage, so playback
        is piped from tail, starting past the bytes already on disk.
        """
        cmd = OmxplayerProcess().build_command({"outfile": FILE_OUT, "pid": 4242})
        assert cmd.startswith('tail -f --pid=4242 --bytes=+0 "' + FILE_OUT + '"')

    def test_local_file_streams_into_omxplayer_stdin(self):
        cmd = OmxplayerProcess().build_command({"outfile": FILE_OUT, "pid": 4242})
        assert cmd.endswith("| " + OMX_CMD + "pipe:0")

    def test_local_file_without_pid_is_reported_not_a_keyerror(self):
        """
        The pid comes only from the yt-dlp stage, and without it there is
        nothing to tail. This used to raise a bare KeyError, which escaped
        start() and left the pipe hung with nothing reported to the parent. It
        is a ProcessException now, so the failure surfaces as an error message.
        """
        from lib.player.processpipe import ProcessException

        with pytest.raises(ProcessException, match="producing process id"):
            OmxplayerProcess().build_command({"outfile": FILE_OUT})

    def test_subtitles_before_outfile(self):
        cmd = OmxplayerProcess().build_command({"outfile": HTTP_OUT, "subtitles": SUBS})
        assert cmd.index("--subtitles") < cmd.index(HTTP_OUT)

    def test_subtitles_with_local_file(self):
        cmd = OmxplayerProcess().build_command(
            {"outfile": FILE_OUT, "pid": 1, "subtitles": SUBS}
        )
        assert "--subtitles '" + SUBS + "'" in cmd
        assert cmd.endswith("pipe:0")


class TestYoutubeDl:
    def test_base_argv_has_the_essential_flags(self):
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        for flag in (
            "--no-part",
            "--no-continue",
            "--no-playlist",
            "--no-progress",
            "--output",
        ):
            assert flag in cmd, flag

    def test_invoked_through_python(self):
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        assert cmd[0] == "python"
        assert cmd[1].endswith(os.path.join("yt_dlp", "__main__.py"))

    def test_url_is_last(self):
        url = SHORT_URL
        assert YoutubeDlProcess(url)._get_cmd({})[-1] == url

    def test_simulate_only_when_streaming_directly(self):
        """
        skip_download() True means the stream is piped to the player, so yt-dlp
        only resolves it. False means the file must be fetched first.
        """
        piped = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        assert "--simulate" in piped
        assert "--dump-single-json" in piped

    def test_download_when_not_skipped(self):
        """
        ITV and openload links are downloaded rather than streamed.
        """
        cmd = YoutubeDlProcess("https://www.itv.com/watch")._get_cmd({})
        assert "--simulate" not in cmd
        assert "--dump-single-json" not in cmd

    def test_youtube_forces_mp4(self):
        cmd = YoutubeDlProcess(YT_URL)._get_cmd({})
        assert "(mp4)" in cmd

    def test_bbc_caps_resolution(self):
        cmd = YoutubeDlProcess(BBC_URL_SAMPLE)._get_cmd({})
        assert "best[height<720]" in cmd

    def test_unknown_host_falls_back_to_best(self):
        cmd = YoutubeDlProcess("http://example.com/v.mp4")._get_cmd({})
        assert "best" in cmd

    def test_format_precedes_the_url(self):
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        assert cmd.index("--format") < cmd.index("--format") + 2
        assert cmd[-1] == SHORT_URL

    def test_args_are_retained_for_ready(self):
        """_ready() reads self.args, set here, to report pid and outfile."""
        proc = YoutubeDlProcess(SHORT_URL)
        args = {}
        proc._get_cmd(args)
        assert proc.args is args


class TestSubtitles:
    def test_movie_form(self):
        proc = SubtitlesProcess({"lang": "eng", "title": "The Matrix", "year": 1999})
        cmd = proc._get_cmd({})
        assert cmd[0].endswith("getsubs.py")
        assert cmd[1] == "eng"
        assert "-t" in cmd and "The Matrix" in cmd
        assert "-y" in cmd and 1999 in cmd

    def test_movie_without_year_or_imdb_omits_them(self):
        proc = SubtitlesProcess({"lang": "eng", "title": "The Matrix"})
        cmd = proc._get_cmd({})
        assert "-y" not in cmd
        assert "-i" not in cmd

    def test_empty_year_is_treated_as_absent(self):
        """Guarded by `and self.subs["year"]`, so a blank field is skipped."""
        proc = SubtitlesProcess({"lang": "eng", "title": "M", "year": None})
        assert "-y" not in proc._get_cmd({})

    def test_movie_with_imdb(self):
        proc = SubtitlesProcess({"lang": "eng", "title": "M", "imdb": "tt0133093"})
        cmd = proc._get_cmd({})
        assert "-i" in cmd and "tt0133093" in cmd

    def test_series_form(self):
        proc = SubtitlesProcess(
            {"lang": "eng", "series": "Some Show", "season": 2, "episode": 5}
        )
        cmd = proc._get_cmd({})
        assert "-t" in cmd and "Some Show" in cmd
        assert "-s" in cmd and 2 in cmd
        assert "-e" in cmd and 5 in cmd

    def test_series_takes_precedence_over_title(self):
        proc = SubtitlesProcess(
            {"lang": "eng", "series": "S", "season": 1, "episode": 1, "title": "T"}
        )
        cmd = proc._get_cmd({})
        assert "S" in cmd
        assert "T" not in cmd

    def test_uses_argv(self):
        proc = SubtitlesProcess({"lang": "eng", "title": "M"})
        assert isinstance(proc._get_cmd({}), list)


class TestPeerflix:
    def test_argv_shape(self):
        cmd = PeerflixProcess("http://x/y.torrent", None)._get_cmd({})
        assert cmd[0] == "node"
        assert "--max-old-space-size=128" in cmd
        assert "/usr/local/bin/peerflix" in cmd

    def test_torrent_is_converted_to_a_magnet(self):
        """
        peerflix needs a magnet; a .torrent url or a bare hash both become one,
        which is what keeps sites that serve html instead of a torrent from
        being queued as a download.
        """
        HASH = "A" * 40
        cmd = PeerflixProcess(HASH, None)._get_cmd({})
        assert cmd[3].startswith("magnet:?xt=urn:btih:")

    def test_magnet_url_left_alone(self):
        magnet = "magnet:?xt=urn:btih:" + "B" * 40
        cmd = PeerflixProcess(magnet, None)._get_cmd({})
        assert cmd[3] == magnet

    def test_file_index_only_when_non_negative(self):
        cmd = PeerflixProcess("http://x/y.torrent", 2)._get_cmd({})
        assert "-i" in cmd
        assert "2" in cmd

    def test_main_file_sends_no_index(self):
        """
        bf_torr_idx=-1 means "the torrent's main file", and peerflixproc
        excludes it via `idx >= 0`, so no -i is emitted and peerflix picks the
        main file itself. Only explicit non-negative indexes are forwarded.
        """
        cmd = PeerflixProcess("http://x/y.torrent", -1)._get_cmd({})
        assert "-i" not in cmd
        assert "-1" not in cmd

    def test_no_index_when_none(self):
        cmd = PeerflixProcess("http://x/y.torrent", None)._get_cmd({})
        assert "-i" not in cmd

    def test_serving_port(self):
        cmd = PeerflixProcess("http://x/y.torrent", None)._get_cmd({})
        assert "-p" in cmd
        assert "9696" in cmd

    def test_args_retained_for_ready(self):
        proc = PeerflixProcess("http://x/y.torrent", None)
        args = {}
        proc._get_cmd(args)
        assert proc.args is args

    def test_builds_cmd_in_constructor(self):
        """Unlike every other stage, peerflix does not need args to build."""
        proc = PeerflixProcess("http://x/y.torrent", 1)
        assert proc._get_cmd({}) is proc.cmd


class TestLocalFile:
    """
    Not an ExternalProcess: it has no command, it just hands a path on. That is
    how a file:// url is played without involving the network at all.
    """

    def test_ready_reports_the_path(self):
        proc = LocalFileProcess("/media/movies/a.mkv")
        q = _capture(proc)
        proc.set_msgq(q, 0)
        proc.start({})
        assert q.get_nowait() == 1  # MSG_PROCESS_READY
        assert q.get_nowait() == 0  # index
        assert q.get_nowait() == {"outfile": "/media/movies/a.mkv"}

    def test_stop_reports_finished(self):
        from lib.player.processpipe import MSG_PROCESS_FINISHED

        proc = LocalFileProcess("/media/movies/a.mkv")
        q = _capture(proc)
        proc.set_msgq(q, 0)
        proc.stop()
        assert q.get_nowait() == MSG_PROCESS_FINISHED

    def test_has_no_shell_flag(self):
        """It never spawns anything."""
        assert not hasattr(LocalFileProcess("/a.mkv"), "shell")


def _capture(proc):
    import queue

    q = queue.Queue()
    return q

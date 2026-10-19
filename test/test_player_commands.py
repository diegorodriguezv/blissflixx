"""
Command construction for every pipeline stage.

Each stage's _get_cmd() builds the command it will run and does no I/O, so the
whole command surface is testable without VLC, peerflix, dlsrv or any
Raspberry Pi. Only the shape of each command is asserted: these tests check
that the right flags, paths and arguments are assembled, not that any binary is
installed. Whether the player actually renders video is a question for the
hardware, not for this suite.

Every backend now exposes build_command(args) directly, which is pure and
depends only on args, so no command here needs a process, a shell or the binary
being installed.
"""

import os
import sys
import unittest.mock as m

import pytest

from lib.locations import BIN_PATH
from lib.player import ythelper
from lib.player.dlsrvproc import DlsrvProcess
from lib.player.localproc import LocalFileProcess
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

    def test_invoked_by_the_interpreter_that_is_running_us(self):
        """
        sys.executable, and not "python" from PATH.

        Those were not always the same interpreter. This stage used whichever
        "python" came first on PATH, so a server started by hand resolved
        yt-dlp's imports from the system python while a server started through
        run.sh used the virtualenv's -- which is why the two machines had
        different dependencies available and neither had all of them.

        sys.executable cannot drift, because blissflixx.py refuses to start
        outside this checkout's virtualenv, so there is only one interpreter it
        could mean by the time this runs.
        """
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        assert cmd[0] == sys.executable, cmd[:3]
        assert cmd[1:3] == ["-m", "yt_dlp"], cmd[:4]

    def test_and_it_is_told_which_javascript_runtime_to_use(self):
        """
        yt-dlp stopped assuming one. With no --js-runtimes it looks for deno
        alone, and on a machine without it YouTube extraction is deprecated and
        returns nothing at all.

        Two are named because neither alone is enough: yt-dlp refuses node below
        version 22, and the node in Raspberry Pi OS is 20, so node by itself was
        a runtime yt-dlp rejected without saying so outside a warning. quickjs
        is the one configure.sh installs and the one that actually clears the
        minimum here.

        Naming a runtime that is not there is safe: yt-dlp says "Ignoring
        unsupported JavaScript runtime(s)" and carries on.
        """
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        runtimes = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--js-runtimes"]
        assert runtimes == ["node", "quickjs"], cmd

    def test_a_cookies_file_is_used_when_there_is_one(self, tmp_path, monkeypatch):
        """
        YouTube answers a Pi with "sign in to confirm you're not a bot", and the
        only way past it is an account that is signed in. There is no browser on
        the box to read cookies from, so the file has to be carried over; see
        the README.

        The file being present is the whole switch, so its absence has to be
        the old behaviour exactly.
        """
        import lib.player.ytdlproc as ytdl_module

        jar = tmp_path / "cookies.txt"
        jar.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")

        monkeypatch.setattr(ytdl_module, "COOKIES_PATH", str(jar))
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        assert cmd[cmd.index("--cookies") + 1] == str(jar), cmd

    def test_no_cookies_file_means_no_cookies_flag(self, tmp_path, monkeypatch):
        import lib.player.ytdlproc as ytdl_module

        monkeypatch.setattr(
            ytdl_module, "COOKIES_PATH", str(tmp_path / "does-not-exist.txt")
        )
        assert "--cookies" not in YoutubeDlProcess(SHORT_URL)._get_cmd({})

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
        assert "--print" in piped
        assert not any(
            "%(url)j" in part for part in piped
        ), "the raw url form came back"
        assert "--dump-single-json" not in piped

    def test_the_url_and_whether_it_is_live_are_printed_separately(self):
        """
        A live stream has no total to show, and VLC answers with the size of its
        sliding window anyway -- 11 on one IPTV channel, 13 two seconds later.
        The only honest source for whether it is live is yt-dlp.

        Two lines, not one object. A field an extractor has no value for comes
        out as a bare NA, and inside an object that makes the whole line
        unparseable, so the URL goes down with the missing flag. is_live is
        missing for some sites -- xnxx reports it not at all -- and a film that
        cannot say whether it is live is still a film.
        """
        cmd = YoutubeDlProcess(SHORT_URL)._get_cmd({})
        templates = [cmd[i + 1] for i, part in enumerate(cmd) if part == "--print"]
        assert len(templates) == 2, templates
        assert any("url" in t for t in templates), templates
        assert any("is_live" in t for t in templates), templates
        for template in templates:
            assert (
                "{" not in template
            ), f"{template!r} reads as one object, which a bare NA would break"

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

    def test_a_techcrunch_article_is_exchanged_for_its_media(self, monkeypatch):
        """
        A TechCrunch video post is an article page, and the article page is not
        media. yt-dlp has no extractor for techcrunch.com and its generic one
        gives up, so every item in the channel failed with "Unsupported URL".

        The page names the JW Player playlist the video is served from, and that
        playlist is playable. The URL yt-dlp is finally given has to be the
        playlist, not the article -- which means the last argument is not the URL
        this was constructed with.
        """
        article = "https://techcrunch.com/video/some-story/"
        monkeypatch.setattr(
            ythelper,
            "get",
            lambda url: m.Mock(
                text="<meta itemprop='contentUrl' "
                "content='https://cdn.jwplayer.com/v2/media/nAuWIDg0'/>"
            ),
        )
        cmd = YoutubeDlProcess(article)._get_cmd({})

        assert cmd[-1] == "https://cdn.jwplayer.com/v2/media/nAuWIDg0", cmd
        assert "techcrunch.com" not in cmd[-1]

    def test_a_jw_playlist_asks_for_a_progressive_file(self, monkeypatch):
        """
        Left alone, "best" ranks the HLS variant playlists above the plain
        files, so it returns another playlist instead of a finished media URL.
        Naming the protocol and the container gets the progressive MP4.
        """
        article = "https://techcrunch.com/video/some-story/"
        monkeypatch.setattr(
            ythelper,
            "get",
            lambda url: m.Mock(text="cdn.jwplayer.com/v2/media/nAuWIDg0"),
        )
        cmd = YoutubeDlProcess(article)._get_cmd({})

        assert "best[protocol=https][ext=mp4]" in cmd, cmd

    def test_an_unresolvable_page_is_left_alone(self, monkeypatch):
        """
        This is a translation, not a gate. A page that cannot be read, or one
        that names no playlist, must reach yt-dlp exactly as it arrived --
        failing here and failing there are the same failure, and the second one
        is the one that reports itself.
        """
        article = "https://techcrunch.com/video/some-story/"

        def boom(url):
            raise Exception("Request returned: 403")

        monkeypatch.setattr(ythelper, "get", boom)
        assert YoutubeDlProcess(article)._get_cmd({})[-1] == article

        monkeypatch.setattr(
            ythelper, "get", lambda url: m.Mock(text="<html>nothing here</html>")
        )
        assert YoutubeDlProcess(article)._get_cmd({})[-1] == article

    def test_other_sites_are_not_touched(self, monkeypatch):
        """
        resolve() must not reach the network for anything it does not recognise,
        or every play would pay for a request it does not need.
        """
        calls = []
        monkeypatch.setattr(ythelper, "get", lambda url: calls.append(url))

        for url in (SHORT_URL, YT_URL, BBC_URL_SAMPLE):
            assert YoutubeDlProcess(url)._get_cmd({})[-1] == url

        assert calls == [], "resolve() fetched a page it did not need"

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

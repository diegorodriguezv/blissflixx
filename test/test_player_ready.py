"""
Output parsing for every pipeline stage.

Each stage's _ready() reads exclusively through self._readline(), so replacing
that one method is the entire seam between "a subprocess printed some lines" and
"the stage decided it is ready or failed". Stubbing it means the real parsing
logic is exercised without VLC, peerflix, dlsrv or getsubs.py ever
running.

What is left untestable here is genuinely about the hardware: whether the
picture and sound come out, and whether subtitles burn in correctly. What is
tested is everything up to the point the bytes are handed to the player.
"""

import types

import pytest

from lib.player.dlsrvproc import DlsrvProcess
from lib.player.pflixproc import PeerflixProcess
from lib.player.processpipe import ProcessException
from lib.player.subsproc import SubtitlesProcess
from lib.player.ytdlproc import YoutubeDlProcess

YTDL_PORT = "9696"


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


def stub_proc(proc, pid=4242):
    """
    YoutubeDlProcess._ready() reads self.proc.pid, which normally exists because
    start() spawned the process before _ready() was called.
    """
    proc.proc = types.SimpleNamespace(pid=pid)
    return proc


class TestDlsrvReady:
    def test_listening_line_makes_the_output_an_http_url(self):
        """
        dlsrv serves the partially downloaded file over HTTP so the player can
        read it while it is still growing. The next stage gets that URL, not
        the local path.
        """
        proc = feed(DlsrvProcess(), "Listening on 0.0.0.0:" + YTDL_PORT)
        proc.args = {"outfile": "/tmp/bf.out"}
        assert proc._ready() == {"outfile": "http://127.0.0.1:" + YTDL_PORT}

    def test_prefix_match_not_exact(self):
        proc = feed(DlsrvProcess(), "Listening")
        proc.args = {"outfile": "/tmp/bf.out"}
        assert proc._ready()["outfile"].startswith("http://127.0.0.1:")

    def test_unexpected_line_is_an_error(self):
        proc = feed(DlsrvProcess(), "something else entirely")
        proc.args = {}
        with pytest.raises(ProcessException, match="something else entirely"):
            proc._ready()


class TestPeerflixListeningAddress:
    """
    The URL handed downstream is the one peerflix actually bound to.

    This used to be composed as "http://127.0.0.1:" + port no matter what
    peerflix printed. peerflix chooses its own address -- the first
    non-internal interface it finds -- so on a Pi with a wired connection it
    reports 192.168.1.x and listens there, and the composed loopback URL is an
    address nothing is listening on. The player then sat there for a minute and
    failed with "cannot connect to 127.0.0.1:9696", which is exactly what a
    torrent stream did on real hardware.
    """

    def _ready_url(self, line):
        proc = feed(PeerflixProcess("http://x/y.torrent", None), line)
        proc.args = {}
        return proc._ready()["outfile"]

    def test_the_lan_address_peerflix_printed_is_used(self):
        line = "server is listening on http://192.168.1.119:9696/"
        assert self._ready_url(line) == "http://192.168.1.119:9696"

    def test_loopback_is_still_honoured_when_that_is_what_is_printed(self):
        line = "server is listening on http://127.0.0.1:9696/"
        assert self._ready_url(line) == "http://127.0.0.1:9696"

    def test_a_trailing_slash_does_not_leak_into_the_url(self):
        line = "server is listening on http://192.168.1.119:9696/"
        assert not self._ready_url(line).endswith("/")

    def test_it_falls_back_to_loopback_when_no_url_is_present(self):
        """
        The old behaviour survives as a last resort, so an unexpected line shape
        degrades to something plausible instead of handing on None.
        """
        proc = feed(PeerflixProcess("http://x/y.torrent", None), "server is listening")
        proc.args = {}
        assert proc._ready()["outfile"] == "http://127.0.0.1:" + YTDL_PORT


class TestPeerflixReady:
    def test_listening_line_makes_the_output_an_http_url(self):
        proc = feed(
            PeerflixProcess("http://x/y.torrent", None),
            "server is listening on 127.0.0.1:" + YTDL_PORT,
        )
        proc.args = {}
        assert proc._ready()["outfile"] == "http://127.0.0.1:" + YTDL_PORT

    def test_bad_response_is_an_error(self):
        proc = feed(PeerflixProcess("http://x/y.torrent", None), "Bad Response: 404")
        proc.args = {}
        with pytest.raises(ProcessException, match="Bad Response"):
            proc._ready()

    def test_html_instead_of_a_torrent_is_reported_clearly(self):
        """
        The original intent is preserved rather than surfaced raw: a site
        serving a block page instead of torrent bytes produces the torrent
        parser's "not a colon at" message.
        """
        proc = feed(
            PeerflixProcess("http://x/y.torrent", None),
            "not a colon at 1:34",
        )
        proc.args = {}
        with pytest.raises(ProcessException, match="Unable to retrieve torrent"):
            proc._ready()


class TestYoutubeDlReady:
    def test_json_url_becomes_the_output(self):
        """Piped playback hands the player the direct stream url."""
        proc = feed(stub_proc(YoutubeDlProcess("u")), '"http://direct/x.mp4"', "false")
        proc.args = {}
        assert proc._ready() == {
            "pid": 4242,
            "outfile": "http://direct/x.mp4",
            "is_live": False,
        }

    def test_a_live_stream_is_flagged_for_the_player(self):
        """
        The player must be told, because it cannot work this out for itself.

        VLC answers get_length for a live stream with the size of its sliding
        window -- 11 seconds on one IPTV channel, 13 two seconds later. A
        number that counts up is not a length, but nothing about the reply says
        so, and telling them apart would mean polling, which is what stopped
        the cli interface answering at all. yt-dlp already knows.
        """
        proc = feed(stub_proc(YoutubeDlProcess("u")), '"http://live/x.m3u8"', "true")
        proc.args = {}
        out = proc._ready()
        assert out["is_live"] is True, out
        assert out["outfile"] == "http://live/x.m3u8", out

    def test_a_site_that_does_not_report_live_still_plays(self):
        """
        xnxx reports no is_live field at all, and yt-dlp prints a bare NA for a
        field it does not have.

        That is the case this shape exists for, and it was found by shipping the
        other one: printed inside a single object, {"url": "...", "is_live": NA}
        is not JSON, so the line cannot be parsed at all and the film never
        starts. An absence of information about whether something is live took
        the video down with it. On its own line a bare NA parses as itself and
        means only that this site does not say.
        """
        proc = feed(stub_proc(YoutubeDlProcess("u")), '"http://hls-cdn/x.m3u8"', "NA")
        proc.args = {}
        out = proc._ready()
        assert out["outfile"] == "http://hls-cdn/x.m3u8", out
        assert out["is_live"] is False, "missing is not the same as live"

    def test_a_warning_between_the_two_lines_does_not_hide_the_answer(self):
        """
        The two answers are printed one after the other, but the player writes
        warnings of its own in between.
        """
        proc = feed(
            stub_proc(YoutubeDlProcess("u")),
            '"http://live/x.m3u8"',
            'WARNING: "-f best" selects the best pre-merged format',
            "true",
        )
        proc.args = {}
        assert proc._ready()["is_live"] is True

    def test_a_url_that_never_arrives_is_an_error_not_a_crash(self):
        proc = feed(stub_proc(YoutubeDlProcess("u")), "NA")
        proc.args = {}
        with pytest.raises(ProcessException, match="No URL in YTDL"):
            proc._ready()

    def test_a_missing_url_inside_an_object_is_still_an_error(self):
        proc = feed(stub_proc(YoutubeDlProcess("u")), '{"url": "NA"}')
        proc.args = {}
        with pytest.raises(ProcessException, match="No URL in YTDL"):
            proc._ready()

    def test_pid_is_reported_for_the_next_stage(self):
        """
        The producer is tailed with `tail -f --pid=<pid>`, so it has to
        know which process to stop watching. A missing pid is what makes the
        local-file branch raise KeyError.
        """
        proc = feed(stub_proc(YoutubeDlProcess("u"), pid=99), '{"url": "http://x"}')
        proc.args = {}
        assert proc._ready()["pid"] == 99

    def test_print_url_string_becomes_the_output(self):
        """
        yt-dlp's --print %(url)j form emits a JSON string, not an object. This is
        the form the player now uses because the full extraction JSON was one
        11 MB line for a video with many automatic caption tracks.
        """
        proc = feed(stub_proc(YoutubeDlProcess("u")), '"http://direct/x.mp4"', "false")
        proc.args = {}
        assert proc._ready()["outfile"] == "http://direct/x.mp4"

    def test_requested_formats_falls_back_to_the_first_entry(self):
        """Merged dash/audio formats have no top-level url."""
        proc = feed(
            stub_proc(YoutubeDlProcess("u")),
            '{"requested_formats": [{"url": "http://v.mp4"}, {"url": "http://a.m4a"}]}',
        )
        proc.args = {}
        assert proc._ready()["outfile"] == "http://v.mp4"

    def test_download_destination_line_yields_the_output_path(self):
        """When yt-dlp writes a file rather than streaming, the path is used."""
        from lib.player.processpipe import OUT_FILE

        proc = feed(
            stub_proc(YoutubeDlProcess("u")),
            "[download] Destination: " + OUT_FILE,
        )
        proc.args = {}
        assert proc._ready()["outfile"] == OUT_FILE

    def test_error_line_raises(self):
        proc = feed(stub_proc(YoutubeDlProcess("u")), "ERROR: something broke")
        proc.args = {}
        with pytest.raises(ProcessException):
            proc._ready()

    def test_json_without_any_url_is_rejected(self):
        proc = feed(stub_proc(YoutubeDlProcess("u")), '{"title": "no url here"}')
        proc.args = {}
        with pytest.raises(ProcessException, match="No URL in YTDL"):
            proc._ready()

    def test_missing_print_url_is_rejected(self):
        proc = feed(stub_proc(YoutubeDlProcess("u")), "null")
        proc.args = {}
        with pytest.raises(ProcessException, match="No URL in YTDL"):
            proc._ready()

    def test_noise_before_the_json_is_skipped(self):
        proc = feed(
            stub_proc(YoutubeDlProcess("u")),
            "[youtube] Extracting URL",
            '{"url": "http://x.mp4"}',
        )
        proc.args = {}
        assert proc._ready()["outfile"] == "http://x.mp4"


class TestYtdlErrorMessages:
    """
    _get_ytdl_err is pure string munging that turns yt-dlp's verbose output into
    something showable. Every branch exists because some specific message was
    ugly enough to warrant a rewrite.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (
                "YouTube said: Video unavailable. This video is no longer available",
                "No longer available",
            ),
            ("Unsupported URL: http://example.com", "Unsupported URL"),
            (
                "HTTP Error 403: FORBIDDEN",
                "This video is not available in your country",
            ),
            # The slice is [:idx + 18], one character short of the period, so
            # the rewrite drops the trailing full stop. Pinned as-is.
            ("abc is not a valid URL.", "abc is not a valid URL"),
            ("ERROR: nested ERROR: inner", "nested ERROR: inner"),
        ],
    )
    def test_rewrites(self, raw, expected):
        assert YoutubeDlProcess("u")._get_ytdl_err(raw) == expected

    def test_plain_message_passes_through(self):
        assert YoutubeDlProcess("u")._get_ytdl_err("just a message") == (
            "just a message"
        )

    def test_empty_message_is_none(self):
        """Nothing to show is better than showing an empty error bubble."""
        assert YoutubeDlProcess("u")._get_ytdl_err("") is None

    def test_whitespace_only_is_none(self):
        assert YoutubeDlProcess("u")._get_ytdl_err("   ") is None

    def test_you_tube_said_prefix_is_stripped(self):
        out = YoutubeDlProcess("u")._get_ytdl_err("YouTube said: private video")
        assert out == "private video"


class TestSubtitlesReady:
    def test_filename_is_returned_as_the_subtitles_arg(self):
        proc = feed(
            SubtitlesProcess({"lang": "eng", "title": "M"}),
            '{"filename": "/tmp/a.srt"}',
        )
        assert proc._ready() == {"subtitles": "/tmp/a.srt"}

    def test_filename_is_remembered_for_cleanup(self):
        """
        stop() deletes the fetched file. If subsfile is not set here, the
        subtitle lingers in /tmp on every play.
        """
        proc = feed(
            SubtitlesProcess({"lang": "eng", "title": "M"}),
            '{"filename": "/tmp/a.srt"}',
        )
        proc._ready()
        assert proc.subsfile == "/tmp/a.srt"

    def test_error_field_becomes_the_message(self):
        proc = feed(
            SubtitlesProcess({"lang": "eng", "title": "M"}), '{"error": "no match"}'
        )
        with pytest.raises(ProcessException, match="no match"):
            proc._ready()

    def test_json_without_filename_or_error_is_rejected(self):
        proc = feed(SubtitlesProcess({"lang": "eng", "title": "M"}), '{"other": 1}')
        with pytest.raises(ProcessException, match="No subtitles found"):
            proc._ready()

    def test_non_json_output_is_rejected(self):
        proc = feed(SubtitlesProcess({"lang": "eng", "title": "M"}), "Traceback...")
        with pytest.raises(ProcessException, match="Subtitles died"):
            proc._ready()

    def test_status_message_is_shown_while_fetching(self):
        proc = SubtitlesProcess({"lang": "eng", "title": "M"})
        assert proc.status_msg() == "FETCHING SUBTITLES"

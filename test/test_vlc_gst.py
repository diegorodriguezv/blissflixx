"""
The VLC and GStreamer backends.

Both were written from invocations measured working on the Pi, so these tests
pin the exact argv rather than the shape of it. Neither cvlc nor gst-launch-1.0
is run: there is no HDMI or KMS hardware here, and kmssink and drm_vout have no
target. What is verified offline is that the right command would be issued, and
that control commands are formed correctly.

v4l2h264dec, which the default GStreamer pipeline needs, is absent on a machine
without the Pi's kernel packages. That is exactly why the decoders are
configurable rather than hardcoded, and why nothing here needs the element.
"""

import socket
import threading
import time
import types
import unittest.mock as m

import pytest

from lib.player.backend import (
    CAP_AUDIO_TRACK,
    CAP_PAUSE,
    CAP_SEEK,
    CAP_STOP,
    CAP_SUBTITLES,
    CAP_VOLUME,
)
from lib.player.gstproc import GStreamerProcess
from lib.player.processpipe import ProcessException
from lib.player.vlcproc import _REPLY_TIMEOUT, VlcProcess

FILE_OUT = "/home/diego/testfiles/h264_1080p.mkv"
HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"
SUBS = "/tmp/blissflixx/movie.srt"
RC_SOCK = "/tmp/blissflixx/test-vlc.sock"


def vlc(args, **config):
    return VlcProcess({"rc_socket": RC_SOCK, **config}).build_command(args)


def gst(args, **config):
    return GStreamerProcess(config).build_command(args)


class TestVlcCommand:
    def test_matches_the_verified_pi_invocation(self):
        """
        The exact set of flags measured working, with the control socket added.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert cmd[0] == "cvlc"
        assert cmd[1:] == [
            "--intf=dummy",
            "--aout=alsa",
            "--alsa-audio-device=hdmi:CARD=vc4hdmi,DEV=0",
            "--vout=drm_vout",
            "--drm-vout-module=vc4",
            "--sub-text-scale=60",
            "--extraintf=rc",
            "--rc-unix=" + RC_SOCK,
            "--no-video-title-show",
            "--play-and-exit",
            FILE_OUT,
        ]

    def test_file_is_the_final_positional(self):
        assert vlc({"outfile": FILE_OUT})[-1] == FILE_OUT

    def test_uses_argv_not_a_shell(self):
        assert VlcProcess().shell is False

    def test_subtitles_when_present(self):
        assert "--sub-file=" + SUBS in vlc({"outfile": FILE_OUT, "subtitles": SUBS})

    def test_no_subtitle_flag_when_absent(self):
        assert not [a for a in vlc({"outfile": FILE_OUT}) if "--sub-file" in a]

    def test_reads_a_growing_http_url(self):
        """VLC handles the dlsrv stage's url itself, unlike omxplayer's tail."""
        assert vlc({"outfile": HTTP_OUT})[-1] == HTTP_OUT

    def test_exits_when_playback_ends(self):
        """
        --play-and-exit is what makes the pipeline report finished instead of
        the player sitting there after the last frame.
        """
        assert "--play-and-exit" in vlc({"outfile": FILE_OUT})


class TestVlcPlatformVariants:
    @pytest.mark.parametrize(
        "label,device",
        [
            ("pi4", "hdmi:CARD=vc4hdmi,DEV=0"),
            ("pi5", "hdmi:CARD=hdmi_zero,DEV=0"),
            ("desktop", "plughw:CARD=PCH,DEV=0"),
        ],
    )
    def test_audio_device_variants(self, settings, label, device):
        from lib.settings import save

        save("player-vlc", {"audio_device": device})
        from lib.player.backends import get_backend

        cmd = get_backend("vlc").build_command({"outfile": FILE_OUT})
        assert "--alsa-audio-device=" + device in cmd, label


class TestVlcControl:
    def _sent(self, action, **config):
        proc = VlcProcess({"rc_socket": RC_SOCK, **config})
        sent = []
        proc._send = lambda command: sent.append(command) or True
        proc.control(action)
        return sent[0] if sent else None

    @pytest.mark.parametrize(
        "action,expected",
        [
            ("pause", "pause"),
            ("resume", "play"),
            ("stop", "quit"),
            ("plus30", "seek 30"),
            ("minus30", "seek -30"),
            ("plus600", "seek 600"),
            ("minus600", "seek -600"),
            ("next_subtitle", "subtitle"),
            ("prev_subtitle", "subtitle"),
            ("show_subtitle", "subtitle"),
            ("hide_subtitle", "subtitle"),
        ],
    )
    def test_action_maps_to_rc_command(self, action, expected):
        assert self._sent(action) == expected

    def test_volume_up_steps_the_level(self):
        """
        The level is tracked from the last known set point, starting at VLC's
        mid-scale default of 256. rc has no query for the current level.
        """
        proc = VlcProcess({"rc_socket": RC_SOCK})
        proc._send = lambda c: True
        proc.control("voldown")
        assert proc._volume == 251
        proc.control("volup")
        assert proc._volume == 256
        proc.control("volup")
        assert proc._volume == 261

    def test_volume_never_goes_below_zero(self):
        proc = VlcProcess({"rc_socket": RC_SOCK})
        proc._send = lambda c: True
        for _ in range(60):
            proc.control("voldown")
        assert proc._volume == 0

    def test_volume_is_capped(self):
        proc = VlcProcess({"rc_socket": RC_SOCK})
        proc._send = lambda c: True
        for _ in range(200):
            proc.control("volup")
        assert proc._volume == proc.opt("volume_max")

    def test_audio_track_sends_nothing(self):
        """
        rc has no track cycling command. Sending nothing is correct; declaring
        the capability absent is what stops the UI offering the buttons.
        """
        assert self._sent("next_audio") is None
        assert self._sent("prev_audio") is None

    def test_audio_track_capability_is_not_declared(self):
        assert VlcProcess().declares(CAP_AUDIO_TRACK) is False

    def test_declared_capabilities(self):
        proc = VlcProcess()
        for cap in (CAP_PAUSE, CAP_STOP, CAP_SEEK, CAP_VOLUME, CAP_SUBTITLES):
            assert proc.declares(cap), cap

    def test_vlc_declares_less_than_mpv(self):
        """
        The dbus omxplayer variant declares even less, and the FIFO variant
        declares everything. VLC sits in between because rc has no track
        cycling.
        """
        from lib.player.mpvproc import MpvProcess
        from lib.player.omxproc import OmxplayerProcess
        from lib.player.omxproc2 import OmxplayerProcess2

        assert (
            len(OmxplayerProcess().capabilities)
            < len(VlcProcess().capabilities)
            < len(OmxplayerProcess2().capabilities)
        )
        assert MpvProcess().capabilities == OmxplayerProcess2().capabilities


class TestVlcTransport:
    def test_command_is_sent_as_bytes_on_the_socket(self):
        """
        The terminator is asserted here and, more usefully, against a real line
        reader in TestVlcAgainstALineReader. This assertion used to expect the
        unterminated payload, which is what VLC could not act on.
        """
        proc = VlcProcess({"rc_socket": RC_SOCK})
        sock = m.MagicMock()
        sock.recv.return_value = b""
        ctx = m.MagicMock()
        ctx.__enter__.return_value = sock
        with (
            m.patch("os.path.exists", return_value=True),
            m.patch("lib.player.vlcproc.socket.socket", return_value=ctx),
        ):
            assert proc._send("pause") is True
        sock.connect.assert_called_once_with(RC_SOCK)
        assert sock.sendall.call_args[0][0] == b"pause\n"

    def test_nothing_sent_when_socket_absent(self):
        """Not running yet, or already gone. Must not raise into the API."""
        proc = VlcProcess({"rc_socket": RC_SOCK})
        with m.patch("os.path.exists", return_value=False):
            assert proc._send("pause") is False

    def test_socket_error_is_not_propagated(self):
        proc = VlcProcess({"rc_socket": RC_SOCK})
        ctx = m.MagicMock()
        ctx.__enter__.side_effect = OSError("gone")
        with (
            m.patch("os.path.exists", return_value=True),
            m.patch("lib.player.vlcproc.socket.socket", return_value=ctx),
        ):
            assert proc._send("quit") is False

    def test_stale_socket_is_removed_before_starting(self, tmp_path):
        sock = tmp_path / "vlc.sock"
        sock.write_text("")
        proc = VlcProcess({"rc_socket": str(sock)})
        proc.proc = None
        proc._remove_socket()
        assert not sock.exists()


class TestVlcReady:
    def test_socket_appearance_means_ready(self, monkeypatch):
        proc = VlcProcess({"rc_socket": RC_SOCK})
        proc.proc = types.SimpleNamespace(poll=lambda: None)
        states = iter([False, True])
        monkeypatch.setattr("os.path.exists", lambda p: next(states, True))
        monkeypatch.setattr("lib.player.vlcproc.time.sleep", lambda s: None)
        proc._ready()

    def test_socket_never_appears_times_out(self, monkeypatch):
        proc = VlcProcess({"rc_socket": RC_SOCK, "start_timeout": 0.05})
        proc.proc = types.SimpleNamespace(poll=lambda: None)
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("lib.player.vlcproc.time.sleep", lambda s: None)
        with pytest.raises(ProcessException, match="timed out"):
            proc._ready()

    def test_early_exit_is_reported(self, monkeypatch):
        proc = VlcProcess({"rc_socket": RC_SOCK})
        proc.proc = types.SimpleNamespace(poll=lambda: 1)
        proc._readline = lambda timeout=None: ""
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("lib.player.vlcproc.time.sleep", lambda s: None)
        with pytest.raises(ProcessException):
            proc._ready()

    def test_decoder_error_is_surfaced(self, monkeypatch):
        proc = VlcProcess({"rc_socket": RC_SOCK})
        proc.proc = types.SimpleNamespace(poll=lambda: 1)
        proc._readline = lambda timeout=None: "no suitable decoder for this stream"
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr("lib.player.vlcproc.time.sleep", lambda s: None)
        with pytest.raises(ProcessException, match="no suitable decoder"):
            proc._ready()


class TestGstreamerCommand:
    def test_matches_the_verified_pi_pipeline(self):
        cmd = gst({"outfile": FILE_OUT})
        assert cmd[0] == "gst-launch-1.0"
        assert cmd[1] == "-q"
        assert cmd[2:] == [
            "filesrc location=" + FILE_OUT,
            "matroskademux",
            "name=demux",
            "demux.video_0",
            "!",
            "queue",
            "!",
            "h264parse",
            "!",
            "v4l2h264dec",
            "!",
            "kmssink",
            "plane-id=98",
            "connector-id=35",
            "demux.audio_0",
            "!",
            "queue",
            "!",
            "avdec_eac3",
            "!",
            "audioconvert",
            "!",
            "audioresample",
            "!",
            "alsasink",
            "device=hdmi:CARD=vc4hdmi,DEV=0",
        ]

    def test_elements_are_separate_argv_words(self):
        """
        Not a shell string. The "!" separators need no escaping this way, so
        shell=False and no quoting are both safe.
        """
        assert GStreamerProcess().shell is False
        assert all(isinstance(a, str) for a in gst({"outfile": FILE_OUT}))
        assert " !" not in " ".join(gst({"outfile": FILE_OUT})).replace(" ! ", "!")

    def test_http_url_switches_to_an_http_source(self):
        """
        filesrc stops at the current end of a file that is still being written,
        so a growing file has to come from dlsrv over http instead.
        """
        cmd = gst({"outfile": HTTP_OUT})
        assert cmd[2] == "souphttpsrc location=" + HTTP_OUT
        assert not any(a.startswith("filesrc") for a in cmd)

    def test_local_file_uses_filesrc(self):
        assert gst({"outfile": FILE_OUT})[2].startswith("filesrc location=")

    def test_no_subtitles(self):
        """
        Text overlay needs the GStreamer compositing path, which is the
        expensive part this backend exists to avoid.
        """
        assert not any("sub" in a.lower() for a in gst({"outfile": FILE_OUT}))

    def test_kms_ids_are_configurable(self):
        cmd = gst({"outfile": FILE_OUT}, plane_id=97, connector_id=36)
        assert "plane-id=97" in cmd
        assert "connector-id=36" in cmd

    def test_decoders_are_configurable(self):
        """
        v4l2h264dec is a Pi kernel plugin and is absent elsewhere, which is why
        the decoder is data rather than a constant.
        """
        cmd = gst(
            {"outfile": FILE_OUT},
            video_decoder="avdec_h264",
            audio_decoder="avdec_aac",
        )
        assert "avdec_h264" in cmd
        assert "avdec_aac" in cmd
        assert "v4l2h264dec" not in cmd

    def test_audio_device_is_configurable(self):
        assert "device=plughw:CARD=PCH,DEV=0" in gst(
            {"outfile": FILE_OUT}, audio_device="plughw:CARD=PCH,DEV=0"
        )


class TestGstreamerHasNoControl:
    def test_declares_nothing(self):
        assert GStreamerProcess().capabilities == frozenset()

    def test_control_is_a_documented_no_op(self):
        """
        gst-launch has no IPC surface. Every action is dropped, and the empty
        capability set is what stops the UI offering the buttons in the first
        place.
        """
        proc = GStreamerProcess()
        for action in (
            "pause",
            "resume",
            "stop",
            "plus30",
            "volup",
            "next_audio",
            "show_subtitle",
        ):
            assert proc.control(action) is None

    def test_stop_still_works_through_the_pipe(self):
        """
        Not through control(): ProcessPipe.stop() SIGKILLs the process group, so
        the UI's stop button is honoured even with no control surface.
        """
        proc = GStreamerProcess()
        proc.proc = None
        proc.stop()


class TestGstreamerReady:
    def test_healthy_pipeline_is_ready(self):
        """Still running after a moment is the only success signal available."""
        proc = GStreamerProcess()
        proc.proc = types.SimpleNamespace(poll=lambda: None)
        proc._ready()

    def test_immediate_exit_is_reported(self, monkeypatch):
        """
        An unknown element makes gst-launch exit at once. Without this check a
        typo in a settings file would become a hang rather than an error.
        """
        proc = GStreamerProcess()
        proc.proc = types.SimpleNamespace(poll=lambda: 2)
        proc._readline = lambda timeout=None: (
            "ERROR: pipeline could not be constructed: no element v4l2h264dec"
        )
        monkeypatch.setattr("lib.player.gstproc.time.sleep", lambda s: None)
        with pytest.raises(ProcessException, match="could not be constructed"):
            proc._ready()


class TestRegistryCoversBoth:
    def test_both_are_registered(self):
        from lib.player.backends import backend_names

        assert "vlc" in backend_names()
        assert "gstreamer" in backend_names()

    def test_describe_flags_the_backend_with_no_controls(self):
        """
        So a caller cannot offer a seek button to something that will ignore it.
        """
        from lib.player.backends import describe

        assert "note" in describe("gstreamer")
        assert "note" not in describe("mpv")


class _FakeRcServer:
    """
    A unix socket that speaks VLC's rc line protocol.

    The point of this is that it only *executes* a command once it has seen a
    complete line. The original _send() wrote "pause" with no terminator, so a
    server that buffers until a newline never executes anything: which is
    exactly what VLC did, and why every control action was silently dropped. A
    MagicMock cannot catch that, because it does not parse anything.
    """

    def __init__(self, path, reply=None, reply_delay=0):
        self.path = str(path)
        self.commands = []
        self.reply = reply
        self.reply_delay = reply_delay
        self.connected = False
        self._buf = ""
        self._ready = threading.Event()
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(1)
        self._sock.settimeout(5)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        # Accept in a loop: _send() opens a fresh connection per command, so the
        # server sees them one after another rather than all on one socket.
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            self.connected = True
            with conn:
                conn.settimeout(5)
                while True:
                    try:
                        chunk = conn.recv(4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    self._buf += chunk.decode("utf-8", "replace")
                    while "\n" in self._buf:
                        line, self._buf = self._buf.split("\n", 1)
                        if line.strip():
                            self.commands.append(line.strip())
                            self._ready.set()
                    if self.reply is not None:
                        if self.reply_delay:
                            time.sleep(self.reply_delay)
                        conn.sendall(self.reply.encode("utf-8"))

    def wait_for_command(self, timeout=5):
        assert self._ready.wait(timeout), "no complete line was ever received"
        return self.commands[0]

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def rc_server(tmp_path):
    """A fake rc server that replies, and records only complete lines."""
    server = _FakeRcServer(
        tmp_path / "vlc.sock", reply="VLC 3.0.23 command line interface\n"
    )
    yield server
    server.close()


class TestVlcAgainstALineReader:
    """
    These replace the assertion that encoded the bug: it checked that the payload
    equalled b"pause", which is precisely the payload VLC cannot act on.
    """

    def test_command_is_executed_by_a_line_reader(self, rc_server):
        proc = VlcProcess({"rc_socket": rc_server.path})
        assert proc._send("pause") is True
        assert rc_server.wait_for_command() == "pause"

    def test_seek_command_reaches_the_reader_intact(self, rc_server):
        proc = VlcProcess({"rc_socket": rc_server.path})
        proc.control("plus30")
        assert rc_server.wait_for_command() == "seek 30"

    def test_negative_seek_is_sent_as_a_signed_number(self, rc_server):
        proc = VlcProcess({"rc_socket": rc_server.path})
        proc.control("minus30")
        assert rc_server.wait_for_command() == "seek -30"

    def test_quit_is_sent(self, rc_server):
        proc = VlcProcess({"rc_socket": rc_server.path})
        proc.control("stop")
        assert rc_server.wait_for_command() == "quit"

    def test_several_commands_in_a_row_are_all_seen(self, rc_server):
        """Reconnect-per-command, so each must carry its own terminator."""
        proc = VlcProcess({"rc_socket": rc_server.path})
        for action in ("pause", "resume", "plus30", "minus30"):
            proc.control(action)
        deadline = time.time() + 5
        while len(rc_server.commands) < 4 and time.time() < deadline:
            time.sleep(0.05)
        assert rc_server.commands == ["pause", "play", "seek 30", "seek -30"]

    def test_volume_command_reaches_the_reader(self, rc_server):
        proc = VlcProcess({"rc_socket": rc_server.path})
        proc.control("volup")
        assert rc_server.wait_for_command() == "vol 261"

    def test_subtitle_toggle_reaches_the_reader(self, rc_server):
        proc = VlcProcess({"rc_socket": rc_server.path})
        proc.control("show_subtitle")
        assert rc_server.wait_for_command() == "subtitle"

    def test_payload_is_newline_terminated(self, rc_server):
        """Spelled out directly, so the requirement is visible on its own."""
        proc = VlcProcess({"rc_socket": rc_server.path})
        with m.patch("lib.player.vlcproc.socket.socket") as factory:
            sock = m.MagicMock()
            sock.recv.return_value = b""
            factory.return_value.__enter__.return_value = sock
            proc._send("pause")
        payload = sock.sendall.call_args[0][0]
        assert payload == b"pause\n"

    def test_reply_is_logged_so_delivery_is_visible(self, rc_server, caplog):
        """
        Distinguishing "written" from "accepted" needs the reply surfaced.

        Read through caplog rather than capsys: cherrypy.log goes via the
        logging module, so it does not appear on the captured stderr stream.
        """
        proc = VlcProcess({"rc_socket": rc_server.path})
        with caplog.at_level("INFO"):
            proc._send("pause")
        rc_server.wait_for_command()
        logged = caplog.text
        assert "VLC RC: pause" in logged
        assert "VLC 3.0.23" in logged

    def test_missing_reply_still_reports_the_command_as_sent(self, tmp_path):
        """
        Best-effort: a player that accepts the command but says nothing must not
        be reported as a failure, and must not block the request thread.
        """
        server = _FakeRcServer(tmp_path / "quiet.sock", reply=None)
        try:
            proc = VlcProcess({"rc_socket": server.path})
            assert proc._send("pause") is True
        finally:
            server.close()

    def test_slow_reply_does_not_hang(self, tmp_path):
        """
        The control request runs on a CherryPy thread, so a player that never
        answers must not hold it for longer than the reply timeout.
        """
        server = _FakeRcServer(tmp_path / "slow.sock", reply="late\n", reply_delay=30)
        try:
            proc = VlcProcess({"rc_socket": server.path})
            started = time.time()
            assert proc._send("pause") is True
            assert time.time() - started < _REPLY_TIMEOUT + 2
        finally:
            server.close()

    def test_no_socket_means_not_delivered(self, tmp_path):
        proc = VlcProcess({"rc_socket": str(tmp_path / "absent.sock")})
        assert proc._send("pause") is False

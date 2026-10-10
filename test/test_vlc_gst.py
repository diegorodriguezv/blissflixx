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

import os
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
    return VlcProcess({**config}).build_command(args)


def gst(args, **config):
    return GStreamerProcess(config).build_command(args)


class TestVlcCommand:
    def test_matches_the_verified_pi_invocation(self):
        """
        The exact set of flags measured working, plus --intf=cli so the
        commands in _send_command() have somewhere to go.

        --vout=drm_vout with the vc4 module is the hardware acceleration
        setup on the Pi and is deliberate, not an arbitrary default.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert cmd[0] == "cvlc"
        assert cmd[1:] == [
            "--intf=cli",
            "--aout=alsa",
            "--alsa-audio-device=hdmi:CARD=vc4hdmi,DEV=0",
            "--vout=drm_vout",
            "--drm-vout-module=vc4",
            "--sub-text-scale=95",
            "--osd",
            "--no-video-title-show",
            "--play-and-exit",
            FILE_OUT,
        ]

    def test_hardware_acceleration_is_pinned_to_drm_vout_on_vc4(self):
        """
        Asserted separately from the full argv so the intent survives someone
        tidying the flag list: this is what makes the Pi decode in hardware.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert "--vout=drm_vout" in cmd
        assert "--drm-vout-module=vc4" in cmd

    def test_the_osd_is_enabled(self):
        """
        Without it a pause or a seek changes nothing visible. The person
        watching has no way to tell whether the button worked, which is the whole
        reason the UI has an on-screen display on the other backends.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert "--osd" in cmd
        # --osd and --no-osd are the same option; having both would be ambiguous.
        assert "--no-osd" not in cmd

    def test_the_cli_interface_is_selected(self):
        """
        Without it VLC has no control surface at all: --intf=dummy means "no
        interface", and with stdin at EOF the cli interface that used to load
        anyway then shut down as soon as it started.
        """
        assert "--intf=cli" in vlc({"outfile": FILE_OUT})
        assert not [a for a in vlc({"outfile": FILE_OUT}) if "--rc-unix" in a]

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
        proc = VlcProcess({**config})
        sent = []
        proc._send_command = lambda command: sent.append(command) or True
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
        ],
    )
    def test_action_maps_to_a_cli_command(self, action, expected):
        assert self._sent(action) == expected

    def test_volume_up_steps_the_level(self):
        """
        The level is tracked from the last known set point, starting at VLC's
        mid-scale default of 256. The cli interface has no query for it either.
        """
        proc = VlcProcess()
        proc._send_command = lambda c: True
        proc.control("voldown")
        assert proc._volume == 251
        proc.control("volup")
        assert proc._volume == 256
        proc.control("volup")
        assert proc._volume == 261

    def test_volume_never_goes_below_zero(self):
        proc = VlcProcess()
        proc._send_command = lambda c: True
        for _ in range(60):
            proc.control("voldown")
        assert proc._volume == 0

    def test_volume_is_capped(self):
        proc = VlcProcess()
        proc._send_command = lambda c: True
        for _ in range(200):
            proc.control("volup")
        assert proc._volume == proc.opt("volume_max")

    def test_audio_track_actions_reach_the_track_interface(self):
        """
        "atrack" lists audio tracks with the active one marked, the same way
        strack does for subtitles. Replaces the pair of tests that asserted
        these were dropped, which was true only while the track interface was
        assumed to be unusable.

        Stepping starts by asking for the listing -- the bare verb -- and only
        then sends an id. With nothing chosen yet the current track is taken to
        be the first real one, so "next" from there wraps to -1 and disables,
        and "prev" from the first track also wraps to -1.
        """
        proc = VlcProcess()
        sent = []

        def collect(command):
            sent.append(command)
            return [
                l.strip()
                for l in (
                    "+----[ audio-es ]",
                    "| -1 - Disable",
                    "| 1 - English - [English] *",
                    "+----[ end of audio-es ]",
                )
            ]

        proc._send_and_collect = collect
        proc.control("next_audio")
        assert sent == ["atrack", "atrack -1"], sent

        # From -1, "prev" wraps forward to the last real track.
        sent.clear()
        proc.control("prev_audio")
        assert sent == ["atrack", "atrack 1"], sent

    def test_hiding_a_subtitle_track_uses_vlcs_disabled_id(self):
        """
        -1 is how VLC spells "off" and it is always present in a listing.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc.control("hide_subtitle")
        assert sent == ["strack -1"]

    def test_showing_a_subtitle_track_sends_a_real_track_id(self):
        """
        This used to send "strack 0", on the assumption that 0 meant the first
        track. A real listing on the Pi is

            | -1 - Disable
            | 2 - English (CC) - [English]

        so there is no 0 in it, and asking for it turned subtitles back on for
        nobody while the log showed a command that had plainly been sent.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc._track_listing["strack"] = [-1, 2]
        proc.control("show_subtitle")
        assert sent == ["strack 2"]

    def test_showing_a_track_returns_to_the_one_that_was_on(self):
        """
        Hide then show comes back to the same track rather than jumping to
        whichever happens to be first.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc._track_listing["strack"] = [-1, 2, 3]
        proc._track_choice["strack"] = 3
        proc.control("show_subtitle")
        assert sent == ["strack 3"]

    def test_showing_a_track_with_no_listing_fetches_one_first(self):
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc._send_and_collect = lambda c: ["| -1 - Disable", "| 4 - Spanish"]
        proc.control("show_subtitle")
        assert sent == ["strack 4"]

    def test_showing_a_track_with_nothing_to_show_sends_nothing(self):
        """
        A listing of nothing but "disable" means there is no subtitle track to
        turn on, so there is no id to send.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc._track_listing["strack"] = [-1]
        proc.control("show_subtitle")
        assert sent == []

    def test_declared_capabilities(self):
        proc = VlcProcess()
        for cap in (
            CAP_PAUSE,
            CAP_STOP,
            CAP_SEEK,
            CAP_VOLUME,
            CAP_SUBTITLES,
            CAP_AUDIO_TRACK,
        ):
            assert proc.declares(cap), cap

    def test_subtitles_and_audio_track_are_declared(self):
        """
        Both are controllable after all: strack and atrack list their tracks and
        the asterisk shows which is active, so visibility can be turned off with
        -1 and back on with a real id. The buttons were hidden when these were
        believed to be missing, which took them away from anyone who could have
        used them.
        """
        proc = VlcProcess()
        assert proc.declares(CAP_SUBTITLES) is True
        assert proc.declares(CAP_AUDIO_TRACK) is True

    def test_volume_is_sent_with_the_verb_the_interface_actually_has(self):
        """
        "vol" is not a cli verb; "volume" is. An unrecognised command is not
        rejected loudly -- the interface just echoes the prompt and says nothing,
        which looks exactly like success. This is what the verb list from
        `help` on the Pi says, and it is why the spelling is pinned.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc._set_volume(300)
        assert sent == ["volume 300"]

    def test_every_verb_sent_is_a_real_cli_verb(self):
        """
        Checked against the verb list `help` prints on the Pi, so a typo cannot
        reach hardware as a silently ignored command.
        """
        known = {
            "add",
            "achan",
            "atrack",
            "chapter",
            "chapter_n",
            "chapter_p",
            "clear",
            "delete",
            "description",
            "enqueue",
            "faster",
            "fastforward",
            "frame",
            "fullscreen",
            "get_length",
            "get_time",
            "get_title",
            "goto",
            "help",
            "info",
            "is_playing",
            "lock",
            "logout",
            "longhelp",
            "loop",
            "move",
            "next",
            "normal",
            "pause",
            "play",
            "playlist",
            "prev",
            "quit",
            "random",
            "rate",
            "repeat",
            "rewind",
            "sd",
            "search",
            "seek",
            "shutdown",
            "slower",
            "snapshot",
            "sort",
            "stats",
            "status",
            "stop",
            "strack",
            "title",
            "title_n",
            "title_p",
            "vcr",
            "vdeinterlace",
            "vdeinterlace_mode",
            "vlm",
            "voldown",
            "volume",
            "volup",
            "vratio",
            "vtrack",
            "vzoom",
        }
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        for action in (
            "pause",
            "resume",
            "stop",
            "plus30",
            "minus30",
            "plus600",
            "minus600",
            "volup",
            "voldown",
        ):
            proc.control(action)
        assert sent, "nothing was sent at all"
        for command in sent:
            verb = command.split()[0]
            assert verb in known, (command, verb)

    def test_still_needs_a_stdin_pipe(self):
        """
        Commands are typed into the process. Without a pipe VLC inherits the
        server's stdin, which is at EOF, and the cli interface exits at once.
        """
        assert VlcProcess().stdin_pipe is True

    def test_vlc_declares_exactly_what_it_can_do(self):
        """
        The set is pinned rather than compared as a length, so gaining or losing
        a capability has to be a deliberate edit here. It matched mpv's set and
        omxplayer-keys' before strack and atrack made that true; the FIFO
        variant declares the same, the dbus one less because it cannot cycle
        tracks at all.
        """
        from lib.player.backend import ALL_CAPABILITIES
        from lib.player.mpvproc import MpvProcess
        from lib.player.omxproc import OmxplayerProcess
        from lib.player.omxproc2 import OmxplayerProcess2

        assert VlcProcess().capabilities == ALL_CAPABILITIES
        assert VlcProcess().capabilities == MpvProcess().capabilities
        assert VlcProcess().capabilities == OmxplayerProcess2().capabilities
        assert len(OmxplayerProcess().capabilities) < len(ALL_CAPABILITIES)


class _FakeStdin:
    """
    Stands in for the write end of the process's stdin pipe.

    Real enough for the assertions that matter -- what was written, and that a
    broken pipe surfaces as False rather than an exception into the API.
    """

    def __init__(self):
        self.written = []
        self.broken = False

    def write(self, data):
        if self.broken:
            raise OSError("broken pipe")
        self.written.append(data)

    def flush(self):
        pass


def _running(config=None):
    """A VlcProcess with a live-looking process and a writable stdin."""
    proc = VlcProcess(config or {})
    proc.proc = types.SimpleNamespace(
        poll=lambda: None, stdin=_FakeStdin(), stdout=None
    )
    return proc


class TestVlcTransport:
    def test_command_is_written_as_bytes_with_a_newline(self):
        """
        The terminator is the whole point, and is asserted here and, more
        usefully, against a real line reader in TestVlcAgainstALineReader.

        The cli interface reads until it sees a newline, executes what it has,
        and replies. Without it a command is neither executed nor rejected: it
        simply sits in the buffer, which is how every control action came to be
        dropped without a word. An earlier version of this test asserted the
        unterminated payload, agreeing with the bug.
        """
        proc = _running()
        proc._replies.put("> pause")
        proc._replies.put(">")
        assert proc._send_command("pause") is True
        assert proc.proc.stdin.written == [b"pause\n"]

    def test_nothing_sent_when_the_process_is_gone(self):
        """Already stopped, or never started. Must not raise into the API."""
        proc = VlcProcess()
        proc.proc = types.SimpleNamespace(poll=lambda: 0)
        assert proc._send_command("pause") is False

    def test_no_process_at_all_is_not_delivered(self):
        proc = VlcProcess()
        assert proc._send_command("pause") is False

    def test_a_broken_pipe_is_not_propagated(self):
        """
        VLC exiting between the check and the write must surface as "not
        delivered", not as an OSError out of a CherryPy request thread.
        """
        proc = _running()
        proc.proc.stdin.broken = True
        assert proc._send_command("quit") is False


class TestVlcReady:
    """
    Readiness is the cli interface announcing itself.

    This used to be the rc socket appearing at a path. That could never happen
    here: Debian trixie's VLC 3.0.23 for armhf ships no librc_plugin.so, so
    --extraintf=rc was ignored and the socket was never created, which is why
    the stage sat at ST_STARTING until it was eventually killed.
    """

    def _lines(self, proc, *lines):
        it = iter(lines)

        def _readline(timeout=None):
            try:
                return next(it)
            except StopIteration:
                raise ProcessException("no more output")

        proc._readline = _readline
        return proc

    def test_the_cli_banner_means_ready(self):
        proc = self._lines(
            _running(),
            "VLC media player 3.0.23 Vetinari",
            "Command Line Interface initialized. Type 'help' for help.",
        )
        proc._ready()
        assert proc._ready_seen is True

    def test_startup_chatter_is_discarded_so_it_is_not_mistaken_for_a_reply(self):
        """
        Reversed deliberately. The banner and version line used to be queued so
        the first command would have context, but the interface says nothing
        until spoken to -- so the first control action came back reporting a ten
        second old startup banner as its own answer:

            VLC CLI: pause -> VLC media player 3.0.23 Vetinari /
            Command Line Interface initialized. Type 'help'

        which reads like a failure and is just staleness. Readiness discards
        everything read on the way past.
        """
        proc = self._lines(
            _running(),
            "VLC media player 3.0.23 Vetinari",
            "Command Line Interface initialized. Type 'help' for help.",
        )
        proc._ready()
        assert proc._replies.empty()

    def test_the_line_observer_follows_the_fresh_queue(self):
        """
        The copier installs on_output_line, so resetting the queue has to rebind
        it. Miss that and every reply from a started player is written to a queue
        nobody reads -- which is the same silence, one layer further in.
        """
        proc = self._lines(
            _running(),
            "Command Line Interface initialized. Type 'help' for help.",
        )
        proc._ready()
        proc.on_output_line("( state playing )")
        assert proc._replies.get_nowait() == "( state playing )"

    def test_banner_that_never_arrives_times_out(self):
        proc = _running({"start_timeout": 0.05})
        proc._readline = lambda timeout=None: (_ for _ in ()).throw(
            ProcessException("nothing yet")
        )
        with pytest.raises(ProcessException, match="cli interface"):
            proc._ready()

    def test_early_exit_is_reported(self):
        proc = VlcProcess()
        proc.proc = types.SimpleNamespace(poll=lambda: 1)
        proc._readline = lambda timeout=None: ""
        with pytest.raises(ProcessException):
            proc._ready()

    def test_an_error_line_does_not_end_startup(self):
        """
        The cli interface does not stop talking when it rejects a line: it reads
        its next command from stdin and carries on. So a message about, say, a
        missing audio filter arrives while the player is still running and still
        playing, and treating the first bad line as fatal would end the stage on
        a complaint that changes nothing.

        This is not hypothetical. The player emitted "cannot add user audio
        filter scaletempo (skipped)" and a stage that acted on the first error
        line died with its own success banner quoted as the failure.
        """
        proc = _running({"start_timeout": 0.2})
        lines = iter(
            [
                "main audio filter error: cannot add user audio filter "
                '"scaletempo" (skipped)',
                "Command Line Interface initialized. Type `help' for help.",
            ]
        )

        def readline(timeout=None):
            try:
                return next(lines)
            except StopIteration:
                raise ProcessException("no more output")

        proc._readline = readline
        proc._ready()
        assert proc._ready_seen is True

    def test_startup_error_lines_are_kept_for_the_failure_message(self):
        """
        They are still recorded, so if the interface genuinely never comes up the
        reason it gave is not lost. "cannot open" is one of the markers that
        means this input is unplayable, so it is the realistic case.
        """
        proc = _running({"start_timeout": 0.1})
        proc._readline = lambda timeout=None: "cannot open input.mkv: No such file"
        with pytest.raises(ProcessException, match="cannot open"):
            proc._ready()

    def test_decoder_error_is_surfaced(self):
        proc = VlcProcess()
        proc.proc = types.SimpleNamespace(poll=lambda: 1)
        proc._readline = lambda timeout=None: "no suitable decoder for this stream"
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


class _FakeCli:
    """
    The other end of a pipe pair, speaking VLC's cli line protocol.

    The point of this is that it only *executes* a command once it has seen a
    complete line, and it echoes the command before answering it. The original
    _send_command() wrote "pause" with no terminator, so a reader that buffers until a
    newline never executes anything: which is exactly what VLC did, and why
    every control action was silently dropped. A MagicMock cannot catch that,
    because it does not parse anything.

    Two real os.pipe() pairs, so what _send_command() writes is what this reads and what
    it writes is what the process would really have seen on stdout:

        commands pipe   process -> VLC
        replies pipe    VLC -> process
    """

    def __init__(self, reply=None, reply_delay=0, echo=True):
        self.commands = []
        self.reply = reply
        self.reply_delay = reply_delay
        self.echo = echo
        self._cmd_r, self._cmd_w = os.pipe()
        self._out_r, self._out_w = os.pipe()
        self._buf = ""
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    # -- the process's side ------------------------------------------------
    def stdin_write(self, data):
        os.write(self._cmd_w, data)

    def read_replies(self):
        """
        Non-blocking drain of whatever VLC has said so far.

        Runs on a daemon thread that outlives the fixture's close(), so a
        descriptor disappearing underneath it is expected rather than an error:
        it just means there is nothing more to read.
        """
        import select

        chunks = []
        while True:
            try:
                ready = select.select([self._out_r], [], [], 0)[0]
            except (OSError, ValueError):
                return ""
            if not ready:
                break
            try:
                chunk = os.read(self._out_r, 4096)
            except OSError:
                return ""
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", "replace"))
        return "".join(chunks)

    def close(self):
        for fd in (self._cmd_r, self._cmd_w, self._out_r, self._out_w):
            try:
                os.close(fd)
            except OSError:
                pass

    # -- VLC's side -------------------------------------------------------
    def _pump(self):
        """
        Buffer until a newline, then execute.

        Deliberately strict: an unterminated payload is never executed and the
        command list stays empty, which is the behaviour under test.
        """
        while True:
            try:
                chunk = os.read(self._cmd_r, 1024)
            except OSError:
                return
            if not chunk:
                return
            self._buf += chunk.decode("utf-8", "replace")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                self.commands.append(line)
                out = ("> " + line + "\n") if self.echo else ""
                if self.reply:
                    if self.reply_delay:
                        time.sleep(self.reply_delay)
                    out += self.reply + "\n"
                try:
                    os.write(self._out_w, out.encode())
                except (OSError, ValueError):
                    # Closed underneath us by the fixture teardown.
                    return

    def wait_for_command(self, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.commands:
                return self.commands[0]
            time.sleep(0.01)
        return None


@pytest.fixture
def cli():
    server = _FakeCli()
    yield server
    server.close()


@pytest.fixture
def proc_for(cli):
    """
    A VlcProcess wired to the fake cli on both sides.

    _wait() is not run, so the reply side is driven the same way it is in
    production: lines arrive through on_output_line, which is what the copier
    calls. The command side is a plain object with write/flush, standing in for
    the process's own stdin.
    """
    proc = VlcProcess()
    stdin = types.SimpleNamespace(write=cli.stdin_write, flush=lambda: None)
    proc.proc = types.SimpleNamespace(poll=lambda: None, stdin=stdin, stdout=None)

    def feed():
        """
        Stand in for the output copier: drain the reply pipe into the queue the
        same way _LineTail.on_line does in _wait().
        """
        pending = ""
        while True:
            text = cli.read_replies()
            if text:
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    if line.strip():
                        proc.on_output_line(line)
            time.sleep(0.01)

    threading.Thread(target=feed, daemon=True).start()
    return proc


class TestVlcAgainstALineReader:
    """
    These replace the assertion that encoded the bug: it checked that the payload
    equalled b"pause", which is precisely the payload VLC cannot act on.
    """

    def test_command_is_executed_by_a_line_reader(self, proc_for, cli):
        assert proc_for._send_command("pause") is True
        assert cli.wait_for_command() == "pause"

    def test_seek_command_reaches_the_reader_intact(self, proc_for, cli):
        proc_for._send_command("seek 30")
        assert cli.wait_for_command() == "seek 30"

    def test_negative_seek_is_sent_as_a_signed_number(self, proc_for, cli):
        proc_for._send_command("seek -30")
        assert cli.wait_for_command() == "seek -30"

    def test_quit_is_sent(self, proc_for, cli):
        proc_for._send_command("quit")
        assert cli.wait_for_command() == "quit"

    def test_several_commands_in_a_row_are_all_seen(self, proc_for, cli):
        for action in ("pause", "seek 30", "volume 300"):
            proc_for._send_command(action)
        deadline = time.time() + 3.0
        while time.time() < deadline and len(cli.commands) < 3:
            time.sleep(0.01)
        assert cli.commands == ["pause", "seek 30", "volume 300"]

    def test_volume_command_reaches_the_reader(self, proc_for, cli):
        proc_for._send_command("volume 300")
        assert cli.wait_for_command() == "volume 300"

    def test_payload_is_newline_terminated(self, proc_for, cli):
        """
        The single most important assertion in this file: an unterminated
        payload must leave the reader having executed nothing at all.

        Only the unterminated write is asserted here, and deliberately not
        followed by a terminated one on the same connection. A line reader keeps
        a partial line buffered, so "pause" followed by "pause\n" executes the
        single command "pausepause" -- which is what VLC would do, and not a
        second bug. The terminated case is covered on a fresh connection by
        test_command_is_executed_by_a_line_reader.
        """
        proc_for.proc.stdin.write(b"pause")
        proc_for.proc.stdin.flush()
        time.sleep(0.3)
        assert cli.commands == []

    def test_a_terminated_command_is_executed_on_the_same_connection(
        self, proc_for, cli
    ):
        """
        The complement of the above: the same connection, properly terminated,
        does get executed. Without this pair the first test could pass against a
        reader that never executed anything at all.
        """
        proc_for._send_command("pause")
        assert cli.wait_for_command() == "pause"

    def test_every_track_action_sends_something(self):
        """
        The six actions that were being dropped when the track interface was
        thought to be unusable.

        Both layers are stubbed, because stepping goes through two of them: the
        listing is fetched by one and the chosen id sent by the other.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda cmd: sent.append(cmd) or True
        proc._send_and_collect = lambda cmd: (sent.append(cmd) or [])
        proc._track_listing["strack"] = [-1, 2]
        proc._track_listing["atrack"] = [-1, 1]
        for action in (
            "show_subtitle",
            "hide_subtitle",
            "next_subtitle",
            "prev_subtitle",
            "next_audio",
            "prev_audio",
        ):
            sent.clear()
            proc.control(action)
            assert sent, action

    def test_reply_is_logged_so_delivery_is_visible(self, proc_for, caplog):
        """
        "written" and "accepted" have to be distinguishable. A log line saying
        only that something was sent would not have told us the interface was
        ignoring it.
        """
        proc = VlcProcess()
        proc.proc = types.SimpleNamespace(
            poll=lambda: None,
            stdin=types.SimpleNamespace(write=lambda d: None, flush=lambda: None),
        )
        proc._replies.put("> pause")
        proc._replies.put("ok")
        with caplog.at_level("INFO"):
            proc._send_command("pause")
        assert "VLC CLI: pause -> ok" in caplog.text

    def test_the_players_own_logging_is_not_mistaken_for_a_reply(
        self, monkeypatch, caplog
    ):
        """
        The interface shares stdout with VLC's logging, and this player writes
        constantly while playing. Lines carrying a thread id are not answers to
        anything and must not appear in the reply.
        """
        monkeypatch.setattr("lib.player.vlcproc._REPLY_TIMEOUT", 0.4)
        proc = _running()
        proc._replies.put("[007070c8] main audio output error: nope")
        proc._replies.put("( state paused )")
        with caplog.at_level("INFO"):
            proc._send_command("pause")
        assert "VLC CLI: pause -> ( state paused )" in caplog.text
        assert "nope" not in caplog.text

    def test_a_command_with_no_answer_reports_none_rather_than_log_noise(
        self, monkeypatch, caplog
    ):
        """
        The honest failure mode: a quiet player and one that ignored the command
        look identical here, so this reports "no reply" instead of dressing up
        whatever happened to be on stdout as a result.
        """
        monkeypatch.setattr("lib.player.vlcproc._REPLY_TIMEOUT", 0.4)
        proc = _running()
        proc._replies.put("[abc1234] some unrelated log line")
        with caplog.at_level("INFO"):
            assert proc._send_command("pause") is True
        assert "VLC CLI: pause -> no reply" in caplog.text

    def test_missing_reply_still_reports_the_command_as_sent(self, monkeypatch, caplog):
        """
        A quiet player is not a failure. The command was written; VLC simply had
        nothing to say, so this must not raise into the API or block the thread.
        """
        monkeypatch.setattr("lib.player.vlcproc._REPLY_TIMEOUT", 0.4)
        proc = _running()
        with caplog.at_level("INFO"):
            assert proc._send_command("pause") is True
        assert "VLC CLI: pause -> no reply" in caplog.text

    def test_slow_reply_does_not_hang(self, monkeypatch, caplog):
        """
        The reply wait is bounded, so a player that never answers cannot hold up
        the request thread that sent the command.
        """
        monkeypatch.setattr("lib.player.vlcproc._REPLY_TIMEOUT", 0.3)
        proc = _running()
        started = time.time()
        assert proc._send_command("pause") is True
        assert time.time() - started < 2.0
        assert "no reply" in caplog.text


class TestTrackListingIsLogged:
    """
    A listing that matches nothing must still be logged.

    This is the difference between a command that was sent and ignored, and one
    that was never sent: both log nothing at all. The unanchored track pattern
    sat unnoticed for exactly that reason -- the output was plainly arriving and
    simply being thrown away, and the log had no trace of it.
    """

    def test_a_parsed_listing_is_logged(self, caplog):
        proc = VlcProcess()
        proc._send_and_collect = lambda c: ["| -1 - Disable", "| 2 - English"]
        with caplog.at_level("INFO"):
            assert proc._track_ids("strack") == [-1, 2]
        assert "VLC CLI: strack -> tracks -1, 2" in caplog.text

    def test_an_unparseable_listing_is_logged_rather_than_silent(self, caplog):
        proc = VlcProcess()
        proc._send_and_collect = lambda c: ["something unexpected"]
        with caplog.at_level("INFO"):
            assert proc._track_ids("strack") == []
        assert "VLC CLI: strack -> something unexpected" in caplog.text


class TestEmbeddedSubtitlesAreEnabledOnStart:
    """
    A file with subtitles in it should show them without being asked.

    VLC starts with no subtitle track selected, so a file with embedded
    subtitles played silently until somebody pressed the subtitle button. VLC
    being the default backend made that the common case rather than the edge
    one.

    OmxplayerProcess2.start() has always called show_subtitle, so this brings VLC
    to the behaviour the project already had, which is what "subtitles are
    mandatory" means in practice.
    """

    def _proc_with_listing(self, lines):
        proc = _running()
        proc.args = {}
        proc._send_and_collect = lambda c: lines
        sent = []
        proc._send_command = lambda c: sent.append(c) or True
        proc._ready_seen = True
        return proc, sent

    def test_starting_the_wait_does_not_block_readiness(self):
        """
        Readiness must not wait on this. The listing is empty until VLC has
        opened the media, so blocking here would hold up the whole pipeline for
        however long a torrent takes to start, for a subtitle track that may not
        even exist.
        """
        proc, sent = self._proc_with_listing(
            ["+----[ spu-es ]", "| -1 - Disable", "| 2 - English (CC)"]
        )
        started = time.time()
        proc._enable_embedded_subtitles()
        assert time.time() - started < 0.5

    def test_the_first_real_track_is_turned_on(self):
        proc, sent = self._proc_with_listing(
            ["+----[ spu-es ]", "| -1 - Disable", "| 2 - English (CC)"]
        )
        proc._wait_for_subtitles({})
        assert sent == ["strack 2"]

    def test_the_wait_gives_up_when_tracks_never_appear(self, monkeypatch):
        """
        Asking once is not enough: at readiness the listing is always empty,
        because VLC has printed its banner but not opened the media. The wait is
        what turns that into a real answer.
        """
        monkeypatch.setattr("lib.player.vlcproc._SUBTITLE_WAIT_TIMEOUT", 0)
        proc, sent = self._proc_with_listing(["+----[ spu-es ]", "| -1 - Disable"])
        proc._wait_for_subtitles({})
        assert sent == []

    def test_a_file_with_no_subtitle_track_sends_nothing(self):
        proc, sent = self._proc_with_listing(["+----[ spu-es ]", "| -1 - Disable"])
        proc._enable_embedded_subtitles()
        assert sent == []

    def test_an_explicitly_chosen_subtitle_file_is_left_alone(self):
        """
        When the UI asked for subtitles by language they arrive as --sub-file and
        VLC selects them itself. Enabling a different embedded track over the top
        would be second-guessing an explicit choice.
        """
        proc, sent = self._proc_with_listing(
            ["+----[ spu-es ]", "| -1 - Disable", "| 2 - English (CC)"]
        )
        proc.args = {"subtitles": "/tmp/blissflixx/episode.srt"}
        proc._enable_embedded_subtitles()
        assert sent == [], "an explicit subtitle choice was overridden"

    def test_the_chosen_track_is_remembered_for_a_later_show(self):
        """
        So hiding subtitles and showing them again returns to this one rather
        than picking whichever track happens to be first.
        """
        proc, sent = self._proc_with_listing(
            ["+----[ spu-es ]", "| -1 - Disable", "| 2 - English (CC)"]
        )
        proc._wait_for_subtitles({})
        assert proc._track_choice["strack"] == 2

    def test_the_start_command_never_carries_peerflixs_remove_flag(self):
        """
        Belt and braces on the deletion change: -r is "--remove, remove files on
        exit" and is why downloads never survived.
        """
        from lib.player.pflixproc import PeerflixProcess

        cmd = PeerflixProcess("magnet:?xt=urn:btih:AAAA", -1).cmd
        assert "-r" not in cmd

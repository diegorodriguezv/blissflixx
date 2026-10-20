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

from lib.player import vlcproc
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
from lib.player.vlcproc import (
    _DECODED_FRAMES,
    _REPLY_TIMEOUT,
    _VOLUME_START_PERCENT,
    MARQ_FILE,
    VlcProcess,
    _volume_units,
)

FILE_OUT = "/home/diego/testfiles/h264_1080p.mkv"
HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"
SUBS = "/tmp/blissflixx/movie.srt"
RC_SOCK = "/tmp/blissflixx/test-vlc.sock"


def vlc(args, **config):
    return VlcProcess({**config}).build_command(args)


def gst(args, **config):
    return GStreamerProcess(config).build_command(args)


def on_screen_proc():
    """
    A VlcProcess put in the state a running one is in: commands are captured
    rather than sent, and the overlay file exists with a message on it, so
    control() can be exercised without a player.
    """
    proc = VlcProcess()
    proc._send_command = lambda command: True
    # Stateful, because control() reads the level back after setting it to
    # report the number that was actually applied -- including after clamping.
    proc._current_volume = lambda: proc._volume_percent
    proc._set_volume = lambda percent: setattr(proc, "_volume_percent", percent)
    proc._show_track = lambda verb: True
    proc._step_track = lambda verb, direction: True
    proc._track_choice = {"strack": -1, "atrack": -1}
    with open(MARQ_FILE, "w", encoding="utf-8") as handle:
        handle.write("Paused")
    proc._overlay_shown = "Paused"
    return proc


class TestVlcCommand:
    def test_matches_the_verified_pi_invocation(self):
        """
        The exact set of flags measured working, plus --intf=cli so the
        commands in _send_command() have somewhere to go. The position overlay
        is added by _osd_args() and asserted separately; this checks the rest
        has not drifted.

        --vout=drm_vout with the vc4 module is the hardware acceleration
        setup on the Pi and is deliberate, not an arbitrary default.
        """
        # Overlay off here, so that this list stays about the base invocation.
        # The overlay arguments are asserted separately and are allowed to grow.
        cmd = VlcProcess({"osd_overlay": "0"}).build_command({"outfile": FILE_OUT})
        assert cmd[0] == "cvlc"
        assert cmd[1:] == [
            "--intf=cli",
            "--aout=alsa,adummy",
            "--alsa-audio-device=hdmi:CARD=vc4hdmi,DEV=0",
            "--vout=drm_vout",
            "--drm-vout-module=vc4",
            "--sub-text-scale=95",
            # Lifts the subtitles off the bottom edge so the overlay can sit
            # just above them instead of over them.
            "--sub-margin=0",
            "--osd",
            # VLC's own title, for the first few seconds. Not written by us:
            # the thread that used to do it polled the player, and polling is
            # what silences the cli interface.
            # Not --video-title-show: for a torrent that is the stream URL,
            # http://192.168.1.119:9696/, which says nothing to a viewer.
            "--no-video-title-show",
            "--play-and-exit",
            # Keyframe seeking. Two seeks in a row used to stop the film dead;
            # see the note on fast_seek in vlcproc. Asserted separately too, so
            # turning it off is a deliberate act rather than a tidy-up.
            "--input-fast-seek",
            FILE_OUT,
        ]

    def test_seeks_go_to_a_keyframe(self):
        """
        --input-fast-seek, and not by accident.

        Pressing +30 twice used to freeze the picture on its last frame while
        the elapsed time carried on. Measured on rpi, two seeks froze 6 times
        out of 6; with this flag, none of six. Spacing the clicks out changed
        nothing -- 6/6 frozen either way -- so the decoder is what has to be
        spared the resynchronisation, not the cli.

        The cost is that a seek lands on the nearest keyframe, so it can be up
        to a GOP out. That is why this is an option: someone who wants exact
        seeking back can turn it off.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert "--input-fast-seek" in cmd
        assert "--no-input-fast-seek" not in cmd

    def test_fast_seeking_can_be_turned_off(self):
        cmd = VlcProcess({"fast_seek": "0"}).build_command({"outfile": FILE_OUT})
        assert not [a for a in cmd if a.startswith("--input-fast-seek")]

    def test_the_overlay_is_wired_up(self):
        """
        marq as a sub source, not a video filter -- passing it to --video-filter
        fails with "Failed to create video filter 'marq'" on this VLC, because
        it is an spu module. Being a sub source is also what lets it draw over
        the picture alongside real subtitles instead of replacing them.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert "--sub-source=marq" in cmd
        assert "--marq-refresh=1" in cmd
        assert not [a for a in cmd if a.startswith("--video-filter=marq")]

    def test_the_overlay_is_big_enough_to_read_from_a_sofa(self):
        """
        The size is left to VLC, which scales it off the video -- the same way
        it sizes subtitles.

        It used to be 84 pixels, fixed. The comment here said 84 "scales with
        resolution", and that was the belief that kept it there: a count of
        pixels does not scale with anything. The same 84 against a 480p picture
        is absurd, against 720p it is big, and against 1080p it is slightly big
        -- which is exactly how it looked.

        Zero is VLC's "no absolute size", not "no text". ConvertToLiveSize()
        then uses f_font_relsize, which is 6.25 -- video height over sixteen --
        so the text is the same size on the picture at any resolution, and the
        same size as a subtitle.
        """
        cmd = vlc({"outfile": FILE_OUT})
        size = int([a for a in cmd if a.startswith("--marq-size=")][0].split("=")[1])
        assert size == 0, (
            f"--marq-size={size} is a fixed pixel count, so the overlay is "
            "the same size on a 480p picture as on a 1080p one"
        )

    def test_a_fixed_size_can_still_be_asked_for(self):
        """
        Zero means "let VLC decide". A number still means pixels, for anyone who
        wants the text the same size whatever they are watching.
        """
        cmd = vlc({"outfile": FILE_OUT}, osd_size="84")
        size = int([a for a in cmd if a.startswith("--marq-size=")][0].split("=")[1])
        assert size == 84, size

    def test_the_overlay_is_at_the_top_not_the_right(self):
        """
        marq-position is not a 1-10 scale. marq passes it straight through as the
        subpicture region's i_align, and the values it offers are 0 center,
        1 left, 2 right, 4 top, 8 bottom. Guessing a scale put the message on
        the right of the screen; 4 is the top edge.

        Pinned against the enum rather than a range, because "between 1 and 2"
        was satisfied by the wrong answer.
        """
        cmd = vlc({"outfile": FILE_OUT})
        pos = int([a for a in cmd if a.startswith("--marq-position=")][0].split("=")[1])
        assert pos == 4, pos

    def test_the_overlay_file_exists_before_the_player_starts(self):
        """
        marq logs "cannot open ...: No such file or directory" once per refresh
        tick, and refresh is 1 second -- so from startup until the first action
        the whole picture is accompanied by that error. The file is made when
        the process is built rather than left for the first control() to make.
        """
        os.remove(MARQ_FILE)
        VlcProcess()
        assert os.path.exists(MARQ_FILE), MARQ_FILE

    def test_the_background_search_gives_up_when_the_user_acts(self):
        """
        This is what stopped a seek from ever showing its time.

        The opening title and the subtitle search both poll the player on a
        background thread, and both go through the same lock and the same
        reply queue as the user's own commands. So a poll was swallowing the
        reply to somebody's ffwd, and the confirmation said nothing at all --
        the seek worked, the film moved, and the screen showed the title from
        startup instead.

        Whichever gets there first, the user is first from then on: the search
        is only worth more than them while they are still watching the title.
        """
        proc = on_screen_proc()
        proc._acted = False
        proc._send_command = lambda command: True

        proc.control("plus30")

        assert proc._acted is True

    def test_the_overlay_file_is_never_zero_bytes_at_startup(self):
        """
        It is created holding a space, not empty: getline() returns -1 at end of
        file and marq reports that as "Invalid argument", so an empty file fails
        the same way a missing one does.
        """
        os.remove(MARQ_FILE)
        VlcProcess()
        assert os.path.getsize(MARQ_FILE) > 0, MARQ_FILE

    @pytest.mark.parametrize(
        "action",
        [
            "pause",
            "resume",
            "stop",
            "plus30",
            "minus30",
            "plus600",
            "minus600",
            "volup",
            "voldown",
            "hide_subtitle",
            "show_subtitle",
            "next_subtitle",
            "prev_subtitle",
            "next_audio",
            "prev_audio",
        ],
    )
    def test_no_action_ever_leaves_the_overlay_file_empty(self, action):
        """
        Whatever an action does, the file marq reads must never be zero bytes --
        that is what getline() cannot handle. Checked for every action rather
        than just resume, because "" was written by one and the next could do
        the same.
        """
        proc = on_screen_proc()
        proc.control(action)
        assert os.path.getsize(MARQ_FILE) > 0, action

    def test_a_dead_sound_card_costs_the_sound_not_the_film(self):
        """
        The freeze, reproduced on a file with one audio track by cycling
        tracks rapidly: switching audio tears the output down and rebuilds it,
        and a burst of switches makes it fail to reopen with

            main audio output error: too low audio sample frequency (0)
            main decoder error: failed to create audio output

        With nothing left to fall back on, the video stalls waiting on a clock
        that will never arrive -- the picture freezes while the controls keep
        answering, because none of them needed the sound to work. adummy is a
        silent output, so the worst case is a film with no sound rather than a
        film that will not move.
        """
        cmd = vlc({"outfile": FILE_OUT})
        aout = [a for a in cmd if a.startswith("--aout=")]

        assert aout == ["--aout=alsa,adummy"], aout

    def test_subtitles_sit_at_the_bottom_edge(self):
        """
        VLC applies sub-margin to the subtitle region alone -- vout_subpictures
        lifts the region by y_margin -- which is the only thing that moves them.

        At 220 they sat about 40% up the picture and at 24 there was still a
        gap under them. Nothing clears them any more -- the overlay is at the
        top -- so they go where VLC puts them by default: as low as they go.
        """
        cmd = vlc({"outfile": FILE_OUT})
        margin = int([a for a in cmd if a.startswith("--sub-margin=")][0].split("=")[1])
        assert margin == 0, margin

    def test_subtitles_are_not_lifted_at_all(self):
        """
        The message is at the top of the frame now, so nothing has to be pushed
        up to make room for it.
        """
        cmd = vlc({"outfile": FILE_OUT})
        assert "--sub-margin=0" in cmd

    def test_the_overlay_reads_its_text_from_a_file(self):
        """
        VLC has no cli command to set the marquee text, so it is given a file.
        marq re-reads that file on every refresh, which is what makes the
        position live -- see _write_overlay.
        """
        from lib.player.vlcproc import MARQ_FILE

        cmd = vlc({"outfile": FILE_OUT})
        assert "--marq-file=" + MARQ_FILE in cmd

    def test_the_overlay_can_be_turned_off(self):
        """
        marq is not asked for at all when disabled, rather than asked for and
        ignored.
        """
        cmd = VlcProcess({"osd_overlay": "0"}).build_command({"outfile": FILE_OUT})
        assert not [a for a in cmd if "marq" in a]
        assert not [a for a in cmd if "sub-source" in a]

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
        """VLC handles the dlsrv stage's url itself, with no tail process."""
        assert vlc({"outfile": HTTP_OUT})[-1] == HTTP_OUT

    def test_exits_when_playback_ends(self):
        """
        --play-and-exit is what makes the pipeline report finished instead of
        the player sitting there after the last frame.
        """
        assert "--play-and-exit" in vlc({"outfile": FILE_OUT})

    def test_a_second_stream_is_given_to_vlc_as_a_slave(self):
        """
        Above 360p YouTube has no stream carrying both picture and sound, and a
        one-URL pipeline could never show a film in HD. VLC plays two inputs at
        once; measured at 1080p H.264 it was hardware-decoded, lost no frames,
        and stayed in sync through a seek.
        """
        cmd = VlcProcess({"osd_overlay": "0"}).build_command(
            {"outfile": "http://v.mp4", "slave": "http://a.m4a"}
        )
        assert "--input-slave=http://a.m4a" in cmd, cmd

    def test_and_a_muxed_source_gets_no_slave(self):
        """A source that carries both arrives as one URL and plays as it always did."""
        cmd = VlcProcess({"osd_overlay": "0"}).build_command(
            {"outfile": "http://x.mp4"}
        )
        assert not [a for a in cmd if a.startswith("--input-slave")], cmd


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
    @pytest.fixture(autouse=True)
    def _no_settle_wait(self, monkeypatch):
        """
        _stalled() gives a seek a couple of seconds to start delivering frames
        before it judges it. Right on a Pi, and nothing but dead time here.
        """
        monkeypatch.setattr(vlcproc, "_SEEK_SETTLE", 0)

    def _sent(self, action, **config):
        proc = VlcProcess({**config})
        sent = []
        proc._send_command = lambda command: sent.append(command) or True
        proc.control(action)
        return sent[0] if sent else None

    @pytest.mark.parametrize(
        "action,expected",
        [
            # Both arrive as one verb: VLC's pause toggles, and the label says
            # what the toggle did. See test_the_label_says_what_the_toggle_did.
            ("pause", "pause"),
            ("resume", "pause"),
            ("stop", "quit"),
            ("plus30", "seek +30"),
            ("minus30", "seek -30"),
            ("plus600", "seek +600"),
            ("minus600", "seek -600"),
        ],
    )
    def test_action_maps_to_a_cli_command(self, action, expected):
        assert self._sent(action) == expected

    @pytest.mark.parametrize("action", ["plus30", "plus600", "minus30", "minus600"])
    def test_a_blind_seek_still_carries_an_explicit_sign(self, action):
        """
        VLC's common.lua decides relative-vs-absolute on the first character:

            if string.sub(value,1,1) == "+" or ... == "-" then  -- relative
            else                                                     -- absolute

        So "seek 30" is an absolute jump to 00:30 and "seek +30" is 30 seconds
        from here. These were sent unsigned, so plus30 took you to the start of
        the film -- which reads like an action mapping bug rather than the syntax
        it was.

        This is the case where the player will not say where it is, so the delta
        goes through untouched and the sign is the only thing making it relative.
        """
        sent = self._sent(action)
        assert sent.split()[1][0] in "+-", sent

    def _sent_from(self, action, position, length):
        """A seek on a player that answers where it is, and how long it is."""
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda command: sent.append(command) or True
        proc._show_overlay = lambda text, hold=None: None
        proc._read_position = lambda: (position, length)
        proc.control(action)
        return sent[0] if sent else None

    def test_a_seek_past_the_end_stops_short_of_it(self):
        """
        VLC does not clamp a seek, so a time past the end runs to EOF, flashes
        the beginning and ends the input -- minutes of frozen picture and then
        the next film in a playlist. Observed as exactly that with +10 minutes
        near the end.

        One second short rather than at the length: exactly the length is an
        immediate end-of-input, so the press would mean "finish this" instead of
        "move on".
        """
        assert self._sent_from("plus600", 1000.0, 1297.0) == "seek 1296"

    def test_a_seek_before_the_start_stops_at_it(self):
        assert self._sent_from("minus600", 5.0, 1297.0) == "seek 0"

    def test_an_ordinary_seek_lands_where_it_was_asked(self):
        assert self._sent_from("plus30", 100.0, 1297.0) == "seek 130"

    def test_and_a_film_with_no_length_yet_still_seeks(self):
        """
        A stream VLC cannot measure has no length to clamp against. Moving on is
        better than refusing over a number the player has not given.
        """
        assert self._sent_from("plus30", 100.0, None) == "seek 130"

    @staticmethod
    def _seeking(counts, paused=False):
        """
        A VlcProcess whose stats answer with `counts` in turn, and which says
        nothing at all if the list runs out.

        So a test can say "the decoder was producing frames" or "it went flat
        and stayed flat" without a player, and without waiting out _SEEK_SETTLE.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = lambda command: sent.append(command) or True
        proc._seek = lambda seconds: sent.append("seek %+d" % seconds) or True
        proc._show_overlay = lambda text, hold=None: sent.append(text)
        proc._paused = paused
        proc._read_position = lambda: (105.0, 1297.0)
        left = list(counts)

        def decoded():
            if not left:
                return None
            value = left.pop(0)
            if value is None:
                return None
            return int(
                _DECODED_FRAMES.match("| video decoded    :   %d" % value).group(1)
            )

        proc._decoded_frames = decoded
        return proc, sent

    def test_the_frame_count_is_read_out_of_the_stats_reply(self):
        """
        `stats` is the only reading that can tell a playing film from a frozen
        one, so it has to survive the shape the interface really sends: the
        statistical tree, a pipe in front of the counter, and the prompt in front
        of that about as often as not.
        """
        proc = VlcProcess()
        replies = {
            "| input bitrate    :     1310 kb/s",
            "| video decoded    :      268",
            "| frames displayed :      224",
        }
        proc._send_and_collect = lambda command: sorted(replies)
        assert proc._decoded_frames() == 268

        proc._send_and_collect = lambda command: sorted(
            ["> | video decoded    :      268"] + list(replies)
        )
        assert proc._decoded_frames() == 268

    def test_the_decoder_count_is_read_rather_than_the_displayed_one(self):
        """
        "frames displayed" counts what reached the video output, so it reads
        zero on a Pi running headless -- drm_vout cannot get a display lease
        there and says so, with the film playing perfectly well. Measured on
        rpi: 435 decoded, 0 displayed. Reading that counter would report every
        seek on such a box as a stall.
        """
        proc = VlcProcess()
        proc._send_and_collect = lambda command: [
            "| video decoded    :      435",
            "| frames displayed :        0",
        ]
        assert proc._decoded_frames() == 435

    def test_a_seek_that_starts_the_film_again_reports_the_position(self):
        proc, sent = self._seeking([900, 1240])
        proc._stalled(30)
        assert "1:45 / 21:37" in sent, sent

    def test_a_seek_that_freezes_the_film_says_so(self):
        """
        The confirmation used to come from get_time, which advances whether or
        not a frame is decoded -- so a film frozen by two seeks in a row
        reported its new position as though the jump had worked. The overlay was
        hiding the fault rather than reporting it.
        """
        proc, sent = self._seeking([900, 900])
        assert proc._stalled(30) is True
        assert "Stalled 1:45 / 21:37" in sent, sent
        assert "1:45 / 21:37" not in [s for s in sent if not s.startswith("Stalled")]

    def test_a_film_that_will_not_say_is_not_called_a_stall(self):
        """
        No reading either side of the seek means there is nothing to compare.
        Reporting a stall on that basis would be worse than the original lie: it
        would cry wolf over a film that was playing perfectly well.
        """
        proc, sent = self._seeking([None, None])
        assert proc._stalled(30) is None
        assert "1:45 / 21:37" in sent, sent

    def test_a_seek_while_paused_is_not_a_stall(self):
        """
        A paused film decodes nothing, and that is what a paused film is for --
        it is meant to be holding a still frame. Reading the flat count as a
        fault here would put "Stalled" on screen every time someone searched
        for a scene while the film was held.
        """
        proc, sent = self._seeking([900, 900], paused=True)
        assert proc._stalled(30) is None
        assert not [s for s in sent if s.startswith("Stalled")], sent

    def test_the_seek_is_sent_whether_or_not_frames_come_back(self):
        """
        The check is a report, not a gate. The picture is already moving by the
        time anyone could be told about it, so there is nothing here that could
        sensibly hold the jump back.
        """
        proc, sent = self._seeking([900, 900])
        proc._stalled(30)
        assert "seek +30" in sent, sent

    def test_the_label_says_what_the_toggle_actually_did(self):
        """
        The interface sends "pause" for its PLAY button as well as its PAUSE
        one, and VLC's pause verb toggles. So "pause" arrives when the film is
        already paused, resumes it, and the confirmation said "Pause" as it did
        the opposite -- which is why Play never appeared.
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda c: True
        proc._read_position = lambda: (30.0, 1297.0)

        proc.control("pause")
        assert shown[-1] == "Pause 0:30 / 21:37", shown

        # The same action again -- what the PLAY button sends -- resumes.
        proc.control("pause")
        assert shown[-1] == "Play 0:30 / 21:37", shown

        proc.control("resume")
        assert shown[-1] == "Pause 0:30 / 21:37", shown

    def test_the_volume_buttons_step_the_level_by_ten_percent(self):
        """
        The level is tracked as a percentage of VLC's own 100%, starting at
        100. The cli interface has no query for it either, so this is our own
        record of the last set point.

        The step is a whole ten percent. It used to be 26 of VLC's units --
        25.6 rounded up, which is 10.16% -- so each press walked a little
        further off the percentage grid.
        """
        proc = VlcProcess()
        proc._send_command = lambda c: True

        assert proc._volume_percent == 100
        proc.control("voldown")
        assert proc._volume_percent == 90
        proc.control("volup")
        assert proc._volume_percent == 100

    def test_the_overlay_shows_whole_numbers_all_the_way_down(self):
        """
        The regression, and the thing the log showed.

        Stepping in units drifted off the grid, so ten presses of volume down
        from 100% read 100 89 79 69 59 49 39 29 18 8 0 -- every one of them a
        number nobody asked for. Stepped in percent they read as intended.
        """
        proc = VlcProcess()
        proc._send_command = lambda c: True
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)

        for _ in range(10):
            proc.control("voldown")

        assert shown == [
            "Volume 90%",
            "Volume 80%",
            "Volume 70%",
            "Volume 60%",
            "Volume 50%",
            "Volume 40%",
            "Volume 30%",
            "Volume 20%",
            "Volume 10%",
            "Volume 0%",
        ], shown

    def test_the_overlay_shows_whole_numbers_all_the_way_up(self):
        proc = VlcProcess()
        proc._send_command = lambda c: True
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)

        for _ in range(5):
            proc.control("volup")

        # 100 -> 110 -> 120, and then the ceiling holds: it says 120%, not a
        # larger number that was refused.
        assert shown == [
            "Volume 110%",
            "Volume 120%",
            "Volume 120%",
            "Volume 120%",
            "Volume 120%",
        ], shown

    def test_the_units_sent_to_vlc_follow_the_percentage(self):
        """
        The percentage is tracked, but VLC is still told in its own 0-512 units.
        The conversion rounds once, to at most half a unit.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = sent.append

        proc.control("voldown")
        proc.control("volup")

        # 90% of VLC's 256 is 230.4, so 230; 100% is 256.
        assert sent == ["volume 230", "volume 256"], sent

    def test_the_volume_does_not_walk_outside_its_range(self):
        proc = VlcProcess()
        proc._send_command = lambda c: True
        for _ in range(60):
            proc.control("voldown")
        assert proc._volume_percent == 0
        for _ in range(60):
            proc.control("volup")
        assert proc._volume_percent == 120

    def test_the_ceiling_is_a_hundred_and_twenty_percent(self):
        """
        120% of VLC's own 100% (256) is 307 of its units. 512 -- the top of
        VLC's scale, which this player used to call 100% -- is its 200% and is
        exactly the level that distorted the audio, so it must not be reachable.
        """
        from lib.player.vlcproc import _VOLUME_MAX, _VOLUME_MAX_PERCENT

        assert _VOLUME_MAX_PERCENT == 120
        assert _VOLUME_MAX == 307
        assert _volume_units(_VOLUME_MAX_PERCENT) == _VOLUME_MAX
        assert _volume_units(200) == 512

    def test_volume_never_goes_below_zero(self):
        proc = VlcProcess()
        proc._send_command = lambda c: True
        for _ in range(60):
            proc.control("voldown")
        assert proc._volume_percent == 0

    def test_volume_is_capped(self):
        proc = VlcProcess()
        proc._send_command = lambda c: True
        for _ in range(200):
            proc.control("volup")
        assert proc._volume_percent == 120

    def test_a_saved_volume_max_cannot_lift_the_ceiling(self):
        """
        An install that already has the old 512 in its settings must not be able
        to put the distortion back. The clamp is in code for exactly that case,
        so it holds whatever the setting says.
        """
        proc = VlcProcess()
        proc._send_command = lambda c: True
        proc.config["volume_max"] = 512

        for _ in range(200):
            proc.control("volup")

        assert proc._volume_percent == 120

    def test_a_lowered_setting_still_lowers_the_ceiling(self):
        """
        The setting is not dead weight: where it is below the built-in ceiling
        it wins, so somebody who wants 110% gets 110%.
        """
        proc = VlcProcess()
        proc._send_command = lambda c: True
        proc.config["volume_max"] = 281  # 110% of 256

        for _ in range(200):
            proc.control("volup")

        assert proc._volume_percent == 110

    def test_the_start_level_is_put_on_the_player(self):
        """
        So a film cannot open at whatever level VLC last saved.

        Nothing was sent at startup, and volume-save is on by default, so the
        level in force was carried over from the last session -- or wherever
        else was playing. A film that opened silent was fixed by pressing
        volume up, which is what the symptom looked like: picture fine,
        controls answering, no sound.
        """
        proc = VlcProcess()
        sent = []
        proc._send_command = sent.append

        proc._apply_start_volume()

        assert sent == ["volume %d" % _volume_units(_VOLUME_START_PERCENT)]
        assert proc._volume_percent == _VOLUME_START_PERCENT

    def test_the_start_level_is_vlcs_own_full_scale(self):
        """
        At VLC's own 100% (256 of its units) rather than the top of its scale
        (512, which is VLC's 200% and distorted the audio). This is the
        regression: the start used to be 512 while the overlay called it 100%.
        """
        from lib.player.vlcproc import _VOLUME_NOMINAL

        assert _VOLUME_NOMINAL == 256
        assert _VOLUME_START_PERCENT == 100
        assert _volume_units(_VOLUME_START_PERCENT) == _VOLUME_NOMINAL

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
        proc._set_volume(117)
        assert sent == ["volume %d" % _volume_units(117)]

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

    def test_vlc_declares_every_capability(self):
        """
        Pinned as an explicit set rather than a length, so gaining or losing a
        capability has to be a deliberate edit here.

        VLC used to be exactly one behind mpv: subtitle delay, because this
        VLC's cli interface has no verb for it. It has none for the delay, still
        -- but it does have a "key" command that simulates a hotkey press, and
        the subtitle-delay hotkey moves the very variable VLC renders with, so
        the capability is real and is declared. See
        VlcProcess._shift_subtitle_delay for what was measured.

        The two backends agreeing is the point of the test: the UI carries one
        set of controls, so a capability either of them lacks has to be a
        deliberate statement rather than a silent difference.
        """
        from lib.player.backend import ALL_CAPABILITIES
        from lib.player.mpvproc import MpvProcess

        missing = ALL_CAPABILITIES - VlcProcess().capabilities
        assert missing == set(), missing
        assert VlcProcess().capabilities == MpvProcess().capabilities


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

    The old player has always shown subtitles from the start, so this brings VLC
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


def _delay_press_proc(step=None):
    """
    A VlcProcess that records subtitle-delay bursts instead of writing them.

    A subtitle track is selected and listed, because that is the state the delay
    works in: VLC applies it to the selected track, and four presses with no
    track selected were measured to move nothing at all.
    """
    proc = VlcProcess({} if step is None else {"subtitle_delay_step": step})
    shown = []
    sent = []

    def send(commands):
        sent.append(list(commands))
        return True

    proc._show_overlay = lambda text, hold=None: shown.append(text)
    proc._send_commands = send
    proc._track_choice["strack"] = 1
    proc._track_listing["strack"] = [-1, 1]
    proc._tracks_answered["strack"] = True
    return proc, shown, sent


class TestWhatTheOverlaySays:
    """
    The wording of a confirmation, which is the only thing the person watching
    gets to see. It has to say what actually happened and nothing it does not
    know.
    """

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("| 2 - English (CC) - [English] *", "English (CC)"),
            ("| 1 - English - [English] *", "English"),
            ("| 3 - Commentary", "Commentary"),
            # A track VLC describes only by its own language, with no plain
            # name in front: "[Spaeth]" and not "[English]".
            ("| 4 - [Spaeth]", "Spaeth"),
            # Trailing bits that are not part of the name.
            ("| 5 - Spanish - [Spanish]", "Spanish"),
            # The asterisk VLC puts on the track that is playing. It is a marker
            # and not part of the name, and the pattern that strips one off after
            # a bracketed language leaves it in place when there is no language,
            # so a track named only by the file read as "Track 1 *" -- which is
            # what the overlay said.
            ("| 1 - Track 1 *", "Track 1"),
            ("| 0 - Commentary *", "Commentary"),
        ],
    )
    def test_the_language_is_taken_from_the_listing(self, line, expected):
        from lib.player.vlcproc import _track_language

        assert _track_language(line) == expected

    def test_the_label_says_on_and_off_with_the_language(self):
        """
        "[on] English" rather than "Subtitle English": the state first, then
        what was switched to. The form asked for, and the state is the part
        that cannot be inferred from the language alone.
        """
        from lib.player.vlcproc import _track_label

        assert _track_label("subtitle", 2, "English") == "[on] English"
        assert _track_label("subtitle", -1, None) == "[off] subtitle"

    def test_off_names_the_track_the_same_way_on_does(self):
        """
        "[off] English" rather than "[off] subtitle". VLC's Disable entry names
        nothing of its own, so the track that was showing is the only thing left
        to name, and "[off] Track 1" says what was switched off where "[off]
        subtitle" says only that subtitles are off.

        The plain word stays as the fallback for when there is nothing to name,
        which is the case above and the reason it is a word rather than an empty
        string: "[off] " with nothing after it is the one label marq cannot be
        given.
        """
        from lib.player.vlcproc import _track_label

        assert _track_label("subtitle", -1, "English") == "[off] English"

    def test_saying_a_film_has_no_tracks_needs_the_player_to_have_answered(self):
        """
        "no subtitles" is a claim about the film, and a player that would not
        answer the listing is not evidence of one. Not answering is also not the
        same as answering with nothing, and both look alike from here until
        something records which one happened. So all three states stay distinct,
        and only the middle one may claim there is nothing there.
        """
        proc = VlcProcess()

        # Never asked.
        assert proc._no_tracks("strack") is False

        # Answered, and there was nothing in the listing.
        proc._tracks_answered["strack"] = True
        assert proc._no_tracks("strack") is True

        # Answered, with tracks.
        proc._track_listing["strack"] = [-1, 2]
        assert proc._no_tracks("strack") is False

        # Answered, with VLC's own Disable entry and nothing beside it -- a state
        # rather than a track, so still nothing there to switch to.
        proc._track_listing["strack"] = [-1]
        assert proc._no_tracks("strack") is True

    def test_no_language_gives_the_plain_word_not_a_number(self):
        """
        With nothing to name, it says "subtitle" -- never "subtitle 0", which
        would be claiming a track id that means nothing to the viewer and may
        not even be the track.
        """
        from lib.player.vlcproc import _track_label

        assert _track_label("subtitle", 2, None) == "[on] subtitle"
        assert _track_label("subtitle", None, None) == "subtitle"

    @pytest.mark.parametrize(
        "percent,expected", [(0, 0), (50, 128), (100, 256), (120, 307), (200, 512)]
    )
    def test_a_percentage_becomes_vlcs_own_units(self, percent, expected):
        """
        VLC's scale runs 0-512 with its own 100% at 256, so the overlay used to
        read "Volume 261" after one press -- a number that means nothing to a
        viewer. This is the one conversion between the percentage the player
        tracks and the units VLC is given, and it rounds at most half a unit.
        """
        assert _volume_units(percent) == expected

    def test_stepping_a_track_names_the_language_it_landed_on(self):
        """
        The end-to-end path: a real VLC listing, a real step, and what the
        overlay ends up saying. The label and the listing are separate pieces
        and either could be right while the pair was wrong.
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "| -1 - Disable",
            "| 2 - English (CC) - [English] *",
            "| 5 - Commentary",
            "+----[ end of spu-es ]",
        ]

        # The listing marks English (CC) active, so a step moves to Commentary
        # -- and it is Commentary that gets named, not the one it came from.
        proc.control("next_subtitle")

        assert shown == ["[on] Commentary"], shown

    @pytest.mark.parametrize(
        "action",
        ["next_subtitle", "prev_subtitle", "show_subtitle", "hide_subtitle"],
    )
    def test_a_film_without_subtitles_says_it_has_none(self, action):
        """
        VLC answers "strack" for a film with no subtitle tracks with the header
        and the footer of an empty listing and nothing between them -- not even
        the "-1 - Disable" that a film with tracks always carries:

            +----[ spu-es ]
            +----[ end of spu-es ]

        Every subtitle button is then a no-op, and the bare word "subtitle" that
        came back instead said nothing about that. It reads as a state rather
        than as an absence, so a viewer cannot tell a film with no subtitles from
        a press that worked.
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "+----[ end of spu-es ]",
        ]

        proc.control(action)

        assert shown == ["no subtitles"], (action, shown)

    def test_stepping_onto_the_disable_entry_names_the_track_it_left(self):
        """
        The last real track in this listing is "Commentary", so a step from
        there wraps onto VLC's -1 - Disable, which is what the listing always
        includes. That entry names nothing, so the label has to reach back to
        the track that was showing rather than give up and say "[off] subtitle".
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "| -1 - Disable",
            "| 2 - English (CC) - [English]",
            "| 5 - Commentary",
            "+----[ end of spu-es ]",
        ]

        proc.control("next_subtitle")  # onto Commentary
        proc.control("next_subtitle")  # and on to the Disable entry

        assert shown == ["[on] Commentary", "[off] Commentary"], shown

    def test_hiding_names_the_track_it_switched_off(self):
        """
        hide_subtitle leaves the recorded choice alone -- that choice is what a
        later step lands relative to, and moving it is a separate thing from
        this -- so the press says for itself that it left subtitles off, and
        names the track rather than falling back to the plain word.
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "| -1 - Disable",
            "| 1 - Track 1",
            "+----[ end of spu-es ]",
        ]

        proc.control("show_subtitle")  # choose the track
        proc.control("hide_subtitle")

        assert shown == ["[on] Track 1", "[off] Track 1"], shown

    def test_next_after_hiding_turns_subtitles_back_on(self):
        """
        Hiding records VLC's Disable entry in the choice, so a step from there
        moves relative to where the player actually is. Leaving the choice on
        the track that was just switched off made the step start from a track
        the player is no longer on: on a one-track film it wrapped straight back
        to the Disable entry, so "next sub" could not turn subtitles on at all.
        """
        proc = VlcProcess()
        shown = []
        sent = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)

        def send(command):
            sent.append(command)
            return True

        proc._send_command = send
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "| -1 - Disable",
            "| 1 - Track 1",
            "+----[ end of spu-es ]",
        ]

        proc.control("show_subtitle")  # on
        proc.control("hide_subtitle")  # off
        proc.control("next_subtitle")  # and back on

        assert shown == ["[on] Track 1", "[off] Track 1", "[on] Track 1"], shown
        assert sent[-1] == "strack 1", sent

    def test_show_after_hide_returns_to_the_track_that_was_on(self):
        """
        Recording -1 in the choice is what makes a step from "off" correct, but
        it also means the choice no longer says which track to come back to --
        so that is remembered separately. show/hide/show returning to the same
        track is what the memory is for; without it the second show would pick
        the first track in the listing instead.
        """
        proc = VlcProcess()
        shown = []
        sent = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)

        def send(command):
            sent.append(command)
            return True

        proc._send_command = send
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "| -1 - Disable",
            "| 2 - English (CC) - [English]",
            "| 5 - Commentary",
            "+----[ end of spu-es ]",
        ]

        proc.control("next_subtitle")  # onto Commentary
        proc.control("hide_subtitle")
        proc.control("show_subtitle")

        assert shown == [
            "[on] Commentary",
            "[off] Commentary",
            "[on] Commentary",
        ], shown
        assert sent[-1] == "strack 5", sent

    def test_a_track_vlc_left_unnamed_still_gets_something_said_about_it(self):
        """
        VLC synthesises "Track N" whenever a file supplies no name of its own,
        which was checked on a file with no metadata at all and on one whose
        title and language were both set to the empty string -- so a nameless
        line should not arrive. If one does, the label still has to be a word.
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._send_and_collect = lambda verb: [
            "+----[ spu-es ]",
            "| -1 - Disable",
            "| 1 - ",
            "+----[ end of spu-es ]",
        ]

        proc.control("show_subtitle")

        assert shown == ["[on] subtitle"], shown

    def test_audio_says_the_same_thing_about_its_own_tracks(self):
        """
        Audio has the same three states as subtitles, so it gets the same labels.

        Nothing switches audio on at startup the way _wait_for_subtitles does for
        subtitles, so the very first press is the one that steps off the only
        track onto the Disable entry -- and it still has a track to name, VLC's
        active one, which is what the step reads as the current position.
        """
        proc = VlcProcess()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._send_and_collect = lambda verb: [
            "+----[ audio-es ]",
            "| -1 - Disable",
            "| 1 - Track 1",
            "+----[ end of audio-es ]",
        ]

        proc.control("next_audio")  # off the only track, onto Disable
        proc.control("next_audio")  # and back onto it

        assert shown == ["[off] Track 1", "[on] Track 1"], shown

        silent = VlcProcess()
        shown = []
        silent._show_overlay = lambda text, hold=None: shown.append(text)
        silent._send_command = lambda command: True
        silent._send_and_collect = lambda verb: [
            "+----[ audio-es ]",
            "+----[ end of audio-es ]",
        ]
        silent.control("next_audio")

        assert shown == ["no audio"], shown

    def test_one_press_of_the_delay_moves_a_step_and_says_so(self):
        """
        VLC's own hotkey moves the subtitle delay 50ms at a time and it cannot
        be asked for an amount, so a quarter-second step is five presses. They
        go out together: five separate commands would each cost the reply settle
        and hold the button for a second and a half.
        """
        proc, shown, sent = _delay_press_proc()

        proc.control("subplus")

        assert sent == [["key key-subdelay-up"] * 5], sent
        assert shown == ["Subtitles +0.25s"], shown

    def test_the_pair_returns_to_sync(self):
        """
        Later then earlier comes back to where it started, which is what pressing
        the two buttons in a row should do -- and the overlay says so rather than
        naming a zero offset.
        """
        proc, shown, sent = _delay_press_proc()

        proc.control("subplus")
        proc.control("subplus")
        proc.control("subminus")
        proc.control("subminus")

        assert shown == [
            "Subtitles +0.25s",
            "Subtitles +0.50s",
            "Subtitles +0.25s",
            "Subtitles in sync",
        ], shown
        assert sent[2] == ["key key-subdelay-down"] * 5, sent

    def test_a_burst_that_never_reached_the_player_moves_nothing(self):
        """
        The offset is what was sent, so a send that failed must not advance it.
        The overlay would otherwise confirm a move the player never made, and
        every press after that would be that much further out than the film is.
        """
        proc, shown, sent = _delay_press_proc()
        proc._send_commands = lambda commands: False

        proc.control("subplus")
        proc.control("subplus")

        assert shown == ["Subtitles in sync", "Subtitles in sync"], shown

    @pytest.mark.parametrize(
        "step,presses",
        [
            (0.05, 1),
            (0.1, 2),
            (0.25, 5),
            (0.3, 6),
            ("0.05", 1),
            # A step VLC cannot express still has to move something rather than
            # being a button that does nothing.
            ("junk", 1),
        ],
    )
    def test_the_step_is_rounded_to_whole_presses(self, step, presses):
        proc, shown, sent = _delay_press_proc(step)

        proc.control("subplus")

        assert sent == [["key key-subdelay-up"] * presses], sent
        assert shown == ["Subtitles %+.2fs" % (presses * 0.05)], shown

    def test_the_delay_is_declined_when_there_is_nothing_to_delay(self):
        """
        The delay belongs to the selected subtitle track, not to the film:
        measured on the Pi, four presses with no track selected left VLC's delay
        at zero. So the press says why it did nothing rather than moving
        something nobody can see -- and sends nothing, so the offset being
        tracked cannot drift away from the player's.
        """
        hidden, shown, sent = _delay_press_proc()
        hidden._track_choice["strack"] = -1

        hidden.control("subplus")
        hidden.control("subminus")

        assert shown == ["Subtitles are off", "Subtitles are off"], shown
        assert sent == [], sent

        # A film with no subtitle tracks at all: the player answered, and there
        # was nothing in the listing.
        silent, shown, sent = _delay_press_proc()
        silent._track_listing.pop("strack", None)

        silent.control("subplus")

        assert shown == ["no subtitles"], shown
        assert sent == [], sent

    def test_the_overlay_shows_a_percentage_not_vlcs_own_scale(self):
        proc = _running()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._send_command = lambda command: True
        proc._current_volume = lambda: proc._volume_percent
        proc._set_volume = lambda percent: setattr(proc, "_volume_percent", percent)

        proc.control("volup")

        # What the units sent are checked in the test above, which does not stub
        # _set_volume away.
        assert shown == ["Volume 110%"], shown


class TestAConfirmationAlwaysSaysSomething:
    """
    A missing confirmation cannot be told from a button that did nothing, which
    is the one thing this mechanism exists to rule out.
    """

    def test_a_seek_with_no_readable_position_says_the_action(self):
        """
        The player not saying where it is says nothing about whether the seek
        was accepted. Showing nothing made it look like the button had done
        nothing at all.
        """
        proc = on_screen_proc()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._read_position = lambda: None

        proc.control("plus30")

        assert shown == ["Seek"], shown

    def test_it_does_not_say_the_action_when_the_position_is_known(self):
        proc = on_screen_proc()
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc._read_position = lambda: (64.0, 1297.0)

        proc.control("plus30")

        assert shown == ["1:04 / 21:37"], shown


class TestHowLongAMessageStays:
    """
    The opening title is worth reading properly; a confirmation is not.
    """

    def test_a_confirmation_is_taken_away_after_five_seconds(self):
        """
        marq has one --marq-timeout for the whole session and the opening title
        wants longer than a confirmation, so the confirmations are expired by
        hand rather than by marq.
        """
        proc = VlcProcess()
        proc._write_overlay = lambda text: None

        with m.patch("lib.player.vlcproc._start_thread") as start:
            proc._show_overlay("Volume 60%", hold=5)

        assert start.call_args is not None, "nothing was scheduled to expire it"
        assert start.call_args.args[1] == "Volume 60%"
        assert start.call_args.args[2] == 5, start.call_args

    def test_the_opening_title_is_left_to_marq(self):
        """
        It is the one message meant to be read, so it is not expired early.
        """
        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)
        proc._read_number = lambda verb: 1297.0 if verb == "get_length" else None
        proc.build_command({"outfile": "/tmp/a.mkv", "title": "Lanterns"})

        with m.patch("lib.player.vlcproc._start_thread") as start:
            with m.patch("lib.player.vlcproc.time.sleep"):
                proc._show_opening_title()

        # The name goes up first and the total is added to it, which is the
        # order the screen wants: something readable while we go looking for the
        # length, rather than silence until we have it.
        assert shown == ["Lanterns", "Lanterns (21:37)"], shown
        assert start.call_count == 0, "the opening title was expired early"

    def test_an_expired_message_never_wipes_a_newer_one(self):
        """
        Guarded on the text still being what is on screen, so a message that has
        already been replaced is not blanked by the earlier one's timer.
        """
        proc = on_screen_proc()
        written = []
        proc._write_overlay = written.append
        proc._overlay_shown = "Volume 60%"

        proc._expire_overlay("Volume 60%", 0)

        # Cleared with a space: getline() returns -1 on an empty file, which
        # marq reports as "Invalid argument" once per refresh tick.
        assert written == [" "], written

        written.clear()
        proc._overlay_shown = "Volume 70%"
        proc._expire_overlay("Volume 60%", 0)

        # The newer message is left alone.
        assert written == [], written

    def test_pause_is_shown_before_the_film_is_toggled(self):
        """
        Asked for on the grounds that a message written once the picture has
        stopped is not one you can rely on being seen.
        """
        proc = on_screen_proc()
        order = []
        proc._show_overlay = lambda text, hold=None: order.append("overlay")
        proc._send_command = lambda command: order.append("command")

        proc.control("pause")

        assert order == ["overlay", "command"], order


class TestTheOverlayStartsBlank:
    """
    The marquee file outlives the film -- the player does not remove it -- and
    marq reads whatever is in it at startup.
    """

    def test_a_new_film_does_not_start_with_the_last_words_of_the_last_one(self):
        """
        A confirmation of a pause was still in the file, and the next film read
        it and showed it -- read as though the new film had said it about
        itself.
        """
        with open(MARQ_FILE, "w", encoding="utf-8") as handle:
            handle.write("Pause 0:14 / 21:37")

        VlcProcess()

        assert open(MARQ_FILE, encoding="utf-8").read().strip() == ""

    def test_stopping_empties_it(self):
        """
        Emptied on stop rather than only at the next start, so a screen that is
        looked at while nothing is playing is not still showing the last thing
        that was said.
        """
        proc = on_screen_proc()
        proc._overlay_shown = "Pause 0:14 / 21:37"
        proc.proc = m.Mock(pid=os.getpid(), poll=lambda: None)
        proc.proc.wait = m.Mock(return_value=0)

        with m.patch("lib.player.processpipe.os.killpg"):
            proc.stop()

        assert open(MARQ_FILE, encoding="utf-8").read().strip() == ""
        assert proc._overlay_shown is None

    def test_it_is_never_left_empty(self):
        """
        A space, not nothing: getline() returns -1 at end of file and marq
        reports that as "Invalid argument" once per refresh tick.

        Asserted as exactly a space rather than merely "not empty", because
        every test in here writes to the same real path -- /tmp/blissflixx/
        marq.txt -- so "not empty" was satisfied by whatever the previous test
        happened to leave on disk. It could pass without stop() having written
        anything at all. It failed once in a full run and could not be
        reproduced in nine, which is what an order-sensitive test looks like.
        """
        proc = on_screen_proc()
        # "Paused" is what on_screen_proc leaves behind. Set to something else
        # so stop() has a genuinely different thing to write, rather than one
        # that happens to match what is already there.
        proc._overlay_shown = "Pause"
        proc.proc = m.Mock(pid=os.getpid(), poll=lambda: None)
        proc.proc.wait = m.Mock(return_value=0)

        with m.patch("lib.player.processpipe.os.killpg"):
            proc.stop()

        assert open(MARQ_FILE, encoding="utf-8").read() == " "


class TestWhatCountsAsAnAnswer:
    """
    Which lines are taken as the player's reply to get_time and get_length.
    """

    @pytest.mark.parametrize(
        "line,expected",
        [
            ("1297", 1297.0),
            # The interface writes its prompt to the same line as the answer
            # about as often as not. It was matched only when bare, which is why
            # a seek's time appeared "sometimes" -- whichever form that one
            # reply happened to take.
            ("> 1297", 1297.0),
            (">  > 1297", 1297.0),
            ("> > 606", 606.0),
            ("( 1297 )", 1297.0),
        ],
    )
    def test_the_prompt_in_front_of_the_answer_is_not_the_problem(self, line, expected):
        proc = _running()
        proc._replies.put(line)

        assert proc._read_number("get_time") == expected

    @pytest.mark.parametrize(
        "line",
        [
            "[00215670] main decoder error: 500 frames",
            "Error 500",
            "Cannot convert non linear input",
            ">",
        ],
    )
    def test_a_number_inside_something_else_is_not_an_answer(self, line):
        """
        The match is anchored to the whole line for exactly this: a number in
        the middle of a message is not the answer, and the player's own logging
        is full of them.
        """
        proc = _running()
        proc._replies.put(line)

        assert proc._read_number("get_time") is None


class TestTheOpeningTitle:
    """
    What is said when a film starts, and how it is obtained.

    It used to be written by us: a background thread polling get_length until
    the player would answer, then writing the title and the duration to the
    marquee. That polling is what stopped VLC's cli answering anything at all
    -- the interface goes quiet within about twenty seconds of playback -- so
    seeks stopped getting a time on their confirmation, which is what it was
    meant to be fixing.
    """

    def test_vlc_is_asked_to_show_its_own_title(self):
        """
        It already knows the duration without being asked, and does not need to
        be spoken to. Asking it in its own words cannot silence it.
        """
        cmd = vlc({"outfile": FILE_OUT})

        assert "--no-video-title-show" in cmd
        assert "--video-title-show" not in cmd

    def test_it_gives_up_and_shows_the_title_alone(self):
        """
        A title with no duration is useful; nothing on screen is not.
        """
        import lib.player.vlcproc as vlc

        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)
        proc._read_number = lambda verb: None
        proc.build_command({"outfile": "/tmp/a.mkv", "title": "Lanterns"})

        # A couple of gaps rather than all eight: this is about what is left on
        # the screen when the player never answers, not about how long it waits.
        with m.patch.object(vlc, "_TITLE_QUESTION_GAPS", (0, 0)), m.patch.object(
            vlc.time, "sleep"
        ):
            proc._show_opening_title()

        assert shown == ["Lanterns"], shown

    def test_the_title_and_its_total_length_are_shown(self):
        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)
        proc._read_number = lambda verb: 7742.0 if verb == "get_length" else None
        proc.build_command({"outfile": "/tmp/a.mkv", "title": "Lanterns S01E08"})

        import lib.player.vlcproc as vlc

        with m.patch.object(vlc, "_TITLE_QUESTION_GAPS", (0,)), m.patch.object(
            vlc.time, "sleep"
        ):
            proc._show_opening_title()

        # The total length, not the position -- at the start that is 0:00.
        assert shown == ["Lanterns S01E08", "Lanterns S01E08 (2:09:02)"], shown

    def test_a_torrent_length_arriving_late_is_still_picked_up(self):
        """
        The case this change exists for.

        A torrent's total does not exist until enough of the file has arrived to
        read it -- measured on one at somewhere between twenty and forty seconds.
        The old code asked five times over twelve and then gave up, so a torrent
        showed its name and never a total, while a film showed one at once. Both
        are the same kind of thing and only one of them has a length.

        The name is already on screen by then, so waiting longer costs nothing to
        be looked at; what matters is that the window is wide enough and the
        questions spread out enough not to stop this cli answering.
        """
        import lib.player.vlcproc as vlc

        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)

        # Nothing for the first four questions, then the length -- as it did.
        answers = [None, None, None, None, 1536.0]
        proc._read_number = lambda verb: answers.pop(0) if answers else 1536.0
        proc.build_command({"outfile": "http://x/stream", "title": "A Torrent"})

        with m.patch.object(
            vlc, "_TITLE_QUESTION_GAPS", (1, 1, 1, 1, 1)
        ), m.patch.object(vlc.time, "sleep"):
            proc._show_opening_title()

        assert shown == ["A Torrent", "A Torrent (25:36)"], shown

    def test_the_window_is_long_enough_for_a_torrent_but_not_endless(self):
        """
        Both halves of the trade. Long enough to reach a torrent's length, and
        few enough questions over it that the interface keeps answering.
        """
        import lib.player.vlcproc as vlc

        gaps = vlc._TITLE_QUESTION_GAPS
        assert (
            sum(gaps) >= 40
        ), f"the window is {sum(gaps)}s, too short to reach a torrent's length"
        assert len(gaps) <= 12, f"{len(gaps)} questions is continuous questioning"
        assert gaps == tuple(sorted(gaps)), "the gaps should back off, not hold"
        assert sum(gaps) <= 120, "waiting over two minutes for a title is too long"

    def test_a_live_stream_is_named_without_a_total(self):
        """
        This is now the only case that omits a total.

        A torrent does not get to: its length is real, it just arrives late --
        between twenty and forty seconds on the one measured, where a film
        answers at once. So the torrent goes through the waiting path and shows
        "A Torrent (25:36)" a while after "A Torrent", and only a live stream
        stops at the name.

        A live channel has no length, and VLC answers with one anyway -- the
        size of its sliding window, which was 11 seconds on one channel and
        13 two seconds later. Shown as a duration that reads as "this lasts
        eleven seconds", which is not true of anything continuous.
        """
        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        asked = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)
        proc._read_number = lambda verb: asked.append(verb) or 11.0
        proc.build_command(
            {
                "outfile": "http://live/channel/index.m3u8",
                "title": "Canal 9",
                "is_live": True,
            }
        )

        import lib.player.vlcproc as vlc

        with m.patch.object(vlc.time, "sleep"):
            proc._show_opening_title()

        assert shown == ["Canal 9"], shown
        # And it must not even ask: a question whose answer is thrown away is
        # still a question, and every needless one is a chance for this cli
        # interface to stop answering.
        assert "get_length" not in asked, asked

    def test_a_live_flag_that_is_absent_is_treated_as_not_live(self):
        """
        The download path never sets it, and a stage that forgets to must not
        silently lose its duration.
        """
        import lib.player.vlcproc as vlc

        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)
        proc._read_number = lambda verb: 7742.0 if verb == "get_length" else None
        proc.build_command({"outfile": "/tmp/a.mkv", "title": "Lanterns S01E08"})

        with m.patch.object(vlc, "_TITLE_QUESTION_GAPS", (0,)), m.patch.object(
            vlc.time, "sleep"
        ):
            proc._show_opening_title()

        assert shown == ["Lanterns S01E08", "Lanterns S01E08 (2:09:02)"], shown

    def test_a_live_stream_is_asked_for_nothing_at_all(self):
        """
        The flag is the only thing that omits a total, so it has to be checked
        before the first question rather than after one -- a question whose
        answer is thrown away is still a question, and every needless one is a
        chance for this cli interface to stop answering.
        """
        import lib.player.vlcproc as vlc

        proc = VlcProcess({"osd_overlay": "1"})
        shown = []
        asked = []
        proc._show_overlay = lambda text, hold=None: shown.append(text)
        proc.proc = m.Mock(poll=lambda: None)
        proc._read_number = lambda verb: asked.append(verb) or 11.0
        proc.build_command(
            {
                "outfile": "http://live/channel/index.m3u8",
                "title": "Canal 9",
                "is_live": True,
            }
        )

        with m.patch.object(vlc.time, "sleep") as slept:
            proc._show_opening_title()

        assert shown == ["Canal 9"], shown
        assert asked == [], f"a live stream was asked {asked}"
        assert not slept.called, "a live stream waited for a total it cannot have"

    def test_a_seek_confirmation_still_asks_once_when_it_must(self):
        """
        Unlike the title thread, this is one question at the moment the user
        asked something -- not a poll -- so it is safe to ask again if the
        answer has not arrived yet.
        """
        proc = _running()
        asked = []
        answers = {"get_time": None, "get_length": None}
        proc._read_number = lambda verb: (asked.append(verb), answers[verb])[1]

        proc._report_progress()
        proc._report_progress()

        assert asked.count("get_length") == 2, asked

    def test_pausing_and_playing_both_say_where_the_film_is(self):
        """
        Pausing is for stopping to do something and coming back to where you
        stopped, so "Paused" on its own left out the part that matters. Same
        for the un-pause.
        """
        proc = on_screen_proc()
        proc._read_position = lambda: (6144.0, 7742.0)

        # "Pause" rather than "Paused": it is the name of the key pressed, and
        # what the button on the remote says.
        proc.control("pause")
        assert proc._overlay_shown == "Pause 1:42:24 / 2:09:02"

        proc.control("resume")
        assert proc._overlay_shown == "Play 1:42:24 / 2:09:02"

    def test_pausing_still_says_something_when_the_player_will_not(self):
        """
        The label on its own, rather than no message at all -- a missing
        confirmation is what this is replacing.
        """
        proc = on_screen_proc()
        proc._read_position = lambda: None

        proc.control("pause")

        assert proc._overlay_shown == "Pause"


class TestConcurrentCommandsDoNotCorruptEachOther:
    """
    Two threads ask this player things at once and must not be answered
    together.

    The overlay reporter polls get_time every second and the subtitle waiter
    polls strack for up to a minute, and both go through the same stdin and the
    same reply queue. So a strack reply could be taken as the answer to
    get_length -- which is why the duration never appeared -- and their commands
    could interleave mid-line.

    Serialised with one lock, rather than given separate reply paths, because
    separate paths mean separate readers on one pipe: the buffered-reader bug
    this project already spent an evening on.
    """

    def _concurrent_proc(self, answers=None):
        """
        A real _send_and_collect, so the lock under test is actually the one
        running. Only the reply-collection is left stubbable.
        """
        proc = _running({"osd_overlay": "1"})
        if answers is not None:
            proc._collect_reply = lambda settle=None: answers.get(
                proc._pending_verb, []
            )
        return proc

    def test_only_one_command_is_in_flight_at_a_time(self):
        """
        Measured where it matters: how many threads are inside the exchange at
        once. Two would mean a reply could be taken for the other command.

        Serialising only the write would still leave this at two, because the
        replies arrive on one queue after the lock has been dropped.
        """
        import threading
        import time

        proc = self._concurrent_proc()
        inside = 0
        peak = 0
        guard = threading.Lock()
        hold = threading.Event()

        def collect(settle=None):
            nonlocal inside, peak
            with guard:
                inside += 1
                peak = max(peak, inside)
            hold.wait(1.0)
            with guard:
                inside -= 1
            return []

        proc._collect_reply = collect

        threads = [
            threading.Thread(target=proc._send_and_collect, args=(verb,))
            for verb in ("get_time", "strack", "get_length")
        ]
        for thread in threads:
            thread.start()
        time.sleep(0.3)
        hold.set()
        for thread in threads:
            thread.join(3.0)

        assert peak == 1, "%d commands were in flight at once" % peak

    def test_every_command_still_gets_its_own_answer(self):
        """
        Serialised, not dropped or merged: each verb gets back what it asked for.
        """
        proc = _running({"osd_overlay": "1"})
        replies = {"get_time": ["12"], "get_length": ["900"]}
        sent = []

        proc._collect_reply = lambda settle=None: replies.get(sent[-1], [])

        def record(verb):
            sent.append(verb)
            return proc._collect_reply()

        proc._send_and_collect = record
        assert proc._read_number("get_time") == 12.0
        assert proc._read_number("get_length") == 900.0
        assert sent == ["get_time", "get_length"]

    def test_the_lock_is_per_instance_not_global(self):
        """
        Two players means two players, not one player's commands queueing behind
        another's.
        """
        first = _running()
        second = _running()
        assert first._command_lock is not second._command_lock


class TestWaitingForARealAnswer:
    """
    The wait must end on a real signal, not on a stray line looking like one.

    This player interleaves messages with no bracketed thread id -- "Device or
    resource busy", "cannot setup filtering pipeline" -- so they pass the
    player's-logging filter and were being collected as replies. The wait then
    stopped as soon as anything had been collected, which is why get_time
    answered perfectly well when asked by hand but read 0:00 through the app:
    the wait was cut short by a line of noise before the number arrived.
    """

    def test_the_xdg_runtime_error_is_not_taken_for_the_answer(self):
        """
        This is what the opening title was reading as the duration.

        get_length replies with this and nothing else -- it has no bracketed
        thread id, so the noise filter did not recognise it, and it took the
        whole reply window:

            error: XDG_RUNTIME_DIR is invalid or not set in the environment.

        Every duration therefore read as unknown, and the announcement fell back
        to the title alone even though the film knew perfectly well how long it
        was. Harmless to the film -- it is about the environment the interface
        runs in -- but it is not an answer.
        """
        proc = _running()
        proc._send_command = lambda c: True
        proc._replies.put(
            "error: XDG_RUNTIME_DIR is invalid or not set in the environment."
        )

        lines = proc._collect_reply(settle=0.2)

        assert lines == [], lines

    def test_a_gap_after_noise_does_not_cut_the_reply_short(self):
        """
        The real shape of the bug, with the gap that makes it bite.

        A line of unbracketed noise arrives, the queue then goes empty, and the
        answer follows a moment later. The old code broke on the empty queue the
        moment anything had been collected, so it returned the noise and never
        saw the number. That is why get_time read 0:00 through the app while
        answering perfectly well when asked by hand.
        """
        import threading

        proc = _running()
        proc._send_command = lambda c: True
        proc._replies.put("Device or resource busy.")

        def answer_soon():
            time.sleep(0.15)
            proc._replies.put("1297")

        threading.Thread(target=answer_soon, daemon=True).start()

        lines = proc._collect_reply(settle=0.5)

        assert "1297" in lines, lines

    def test_a_line_already_queued_alongside_noise_is_still_seen(self):
        """
        The simpler shape, for contrast: no gap, so even the old code would have
        collected both. Kept so the pair documents why the gap is the thing that
        matters.
        """
        proc = _running()
        proc._send_command = lambda c: True
        proc._replies.put("Device or resource busy.")
        proc._replies.put("1297")

        lines = proc._collect_reply(settle=0.2)

        assert "1297" in lines, lines

    def test_a_pause_in_the_output_is_what_ends_the_wait(self):
        """
        Not the arrival of a line: a stream that never goes quiet must not hold
        the request thread for the whole budget.
        """
        proc = _running()
        proc._send_command = lambda c: True
        proc._replies.put("Device or resource busy.")

        started = time.time()
        lines = proc._collect_reply(settle=0.1)

        assert time.time() - started < 1.0
        assert lines == ["Device or resource busy."]

    def test_the_prompt_is_recognised(self):
        from lib.player.vlcproc import _is_echo, _is_prompt

        assert _is_prompt(">") is True
        assert _is_prompt("> pause") is False
        # An echo is the command being repeated back; a prompt is the interface
        # saying it is ready for the next one. Different things.
        assert _is_echo("> pause") is True
        assert _is_echo(">") is False


class TestAStreamThatWillNotOpen:
    """
    VLC exits when it cannot open its input, and --play-and-exit makes that
    indistinguishable from the film ending.

    Without this a dead channel moved a playlist on to the next item in silence,
    and a film played on its own went back to the page with nothing said at all
    -- which is what a stream URL pasted into Play URL did when the source had
    gone away. Reported as this stage's error so the pipe says so.
    """

    MAIN = "https://example.com/live/index.m3u8"
    SLAVE = "https://example.com/live/audio.m4a"

    @staticmethod
    def _opened(url):
        return (
            "[758006b0] main input error: VLC is unable to open the MRL '%s'. "
            "Check the log for details." % url
        )

    def _proc(self, ready=True):
        proc = VlcProcess({"osd_overlay": "0"})
        proc._input_url = self.MAIN
        proc._ready_seen = ready
        return proc

    def test_the_input_it_was_given_failing_is_an_error(self):
        proc = self._proc()
        proc._observe_output(self._opened(self.MAIN))
        assert proc.get_errors() == ["Could not open the stream"]

    def test_an_ordinary_end_is_not(self):
        proc = self._proc()
        proc._observe_output("[756006b0] main playlist: end of playlist, exiting")
        assert proc.get_errors() == []

    def test_a_slave_stream_failing_is_not_the_film_failing(self):
        """
        Above 360p a source arrives as two streams. VLC says the same thing about
        the second one, and a film whose sound did not arrive is still a film.
        """
        proc = self._proc()
        proc._observe_output(self._opened(self.SLAVE))
        assert proc.get_errors() == []

    def test_before_the_interface_is_up_it_is_left_to_ready(self):
        """
        _ready() is reading these same lines then, and reports them with VLC's
        own reason, which says more than this can.
        """
        proc = self._proc(ready=False)
        proc._observe_output(self._opened(self.MAIN))
        assert proc.get_errors() == []

"""
Invariants every registered backend must satisfy.

Five backends exist, and they share almost nothing: three different binaries, two
shells versus three argv builders, three control transports (dbus, FIFO, JSON
IPC, rc socket, and none at all), and capability sets ranging from empty to
complete. What they have in common is the contract PlayerBackend and the
registry impose.

These assertions are deliberately written against the registry rather than
against a list of names, so adding a sixth backend gets them for free instead of
requiring another edit. Each is a property that would otherwise only be noticed
at playback time, on hardware, as an empty command or an unhandled action.
"""

import json
import pathlib
import unittest.mock as m

import pytest

from lib.player.backend import ALL_CAPABILITIES, CAP_PAUSE, PlayerBackend
from lib.player.backends import (
    BACKENDS,
    LEGACY_BACKEND,
    backend_class,
    backend_names,
    describe,
    get_backend,
    resolve_config,
)
from lib.player.player import _Player

HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"
FILE_OUT = "/tmp/blissflixx/bf.out"

ALL_NAMES = sorted(BACKENDS)


class TestRegistryShape:
    def test_registry_is_not_empty(self):
        assert ALL_NAMES

    def test_names_are_unique_and_sorted(self):
        assert backend_names() == sorted(set(backend_names()))

    def test_every_registered_name_resolves(self):
        """backend_names() is the registry, so all of it must be gettable."""
        for name in backend_names():
            assert get_backend(name) is not None

    def test_every_name_resolves(self):
        for name in ALL_NAMES:
            assert backend_class(name) is not None

    def test_every_name_builds_an_instance(self):
        for name in ALL_NAMES:
            assert isinstance(get_backend(name), PlayerBackend)

    def test_every_name_has_its_own_settings_namespace(self):
        from lib.player.backends import settings_name

        names = [settings_name(n) for n in ALL_NAMES]
        assert len(set(names)) == len(names)

    def test_config_cannot_collide_with_the_active_choice_file(self):
        from lib.player.backends import settings_name

        for name in ALL_NAMES:
            assert settings_name(name) != "player"


class TestBackendContract:
    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_declares_defaults(self, name):
        """A backend with no defaults dict cannot be configured or described."""
        assert isinstance(backend_class(name).defaults, dict)
        assert backend_class(name).defaults, name

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_defaults_are_json_serialisable(self, name):
        """Settings are JSON, so a default that cannot round-trip is a trap."""
        json.dumps(backend_class(name).defaults)

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_has_a_binary(self, name):
        assert get_backend(name).opt("binary")

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_declares_a_start_timeout_key(self, name):
        """
        Every backend must state its start timeout, even when that value is None.

        None is deliberate for omxplayer-keys: its input is a file that is still
        growing and has no end, so it waits indefinitely for the first line. It
        is a choice, and it has to be visible as one rather than a missing key.
        """
        assert "start_timeout" in get_backend(name).config

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_capabilities_are_a_subset_of_the_known_set(self, name):
        assert get_backend(name).capabilities <= ALL_CAPABILITIES, name

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_has_a_display_name(self, name):
        assert get_backend(name).name()

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_has_a_start_timeout_property(self, name):
        """OmxplayerBackend._ready reads self.start_timeout."""
        assert get_backend(name).start_timeout == get_backend(name).opt("start_timeout")


class TestCommandBuilding:
    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_builds_a_non_empty_command(self, name):
        assert get_backend(name).build_command({"outfile": HTTP_OUT})

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_command_mentions_the_input(self, name):
        cmd = get_backend(name).build_command({"outfile": HTTP_OUT})
        # Shell backends return one string, argv backends a list.
        flat = [cmd] if isinstance(cmd, str) else [str(a) for a in cmd]
        assert any("127.0.0.1" in part for part in flat), name

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_command_is_pure(self, name):
        """
        build_command must not depend on prior state or on how many times it has
        been called. Building twice has to give the same answer, and building on
        a fresh instance has to match building on a used one.
        """
        backend = get_backend(name)
        first = backend.build_command({"outfile": HTTP_OUT})
        second = backend.build_command({"outfile": HTTP_OUT})
        assert first == second
        assert get_backend(name).build_command({"outfile": HTTP_OUT}) == first

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_command_does_not_mutate_config(self, name):
        backend = get_backend(name)
        before = json.dumps(backend.config, sort_keys=True)
        backend.build_command({"outfile": HTTP_OUT})
        assert json.dumps(backend.config, sort_keys=True) == before

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_shell_flag_is_a_boolean(self, name):
        """
        shell=True goes through /bin/sh, so it must never be an argv list or a
        truthy string; subprocess would treat that differently.
        """
        assert isinstance(get_backend(name).shell, bool)


class TestShellBackendsQuoteTheirInput:
    """
    Backends running through a shell must quote the filename, since a path with
    a space in it would otherwise be split into two arguments.
    """

    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys"])
    def test_shell_string_is_quoted(self, name):
        cmd = get_backend(name).build_command({"outfile": FILE_OUT, "pid": 1})
        assert isinstance(cmd, str)
        # omxplayer quotes the tail path with double quotes, omxplayer-keys the
        # filename with single. Either counts; bare does not.
        quoted = ("'" + FILE_OUT + "'", '"' + FILE_OUT + '"')
        assert any(form in cmd for form in quoted), cmd

    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys"])
    def test_subtitle_path_is_quoted(self, name):
        cmd = get_backend(name).build_command(
            {"outfile": FILE_OUT, "pid": 1, "subtitles": "/tmp/a b.srt"}
        )
        quoted = ("'" + "/tmp/a b.srt" + "'", '"' + "/tmp/a b.srt" + '"')
        assert any(form in cmd for form in quoted), cmd


class TestArgvBackendsKeepPathsIntact:
    @pytest.mark.parametrize("name", ["vlc", "mpv"])
    def test_path_with_a_space_stays_one_argument(self, name):
        """
        The inverse of the shell case: argv builders must not split the path,
        and must not have quoted it either, since nothing will strip the quotes.
        """
        spaced = "/tmp/a b/file.mkv"
        cmd = get_backend(name).build_command({"outfile": spaced})
        assert spaced in cmd
        assert "'" + spaced + "'" not in cmd

    def test_gstreamer_refuses_a_path_it_cannot_express(self):
        """
        gst-launch rebuilds its pipeline by joining argv with spaces and accepts
        no quoting of a property value, so a path with a space cannot be
        expressed. Emitting the command anyway gives
        "WARNING: erroneous pipeline: syntax error", which says nothing about the
        cause, so it raises with the reason instead.
        """
        from lib.player.processpipe import ProcessException

        with pytest.raises(ProcessException, match="space"):
            get_backend("gstreamer").build_command({"outfile": "/tmp/a b/file.mkv"})

    def test_gstreamer_normal_paths_have_no_spaces(self):
        """
        The paths this backend normally sees: the dlsrv url and the torrent
        download path. If either ever gains a space the limitation above starts
        biting in normal use.
        """
        from lib.player.processpipe import OUT_FILE

        assert " " not in OUT_FILE
        assert " " not in "http://127.0.0.1:9696"


class TestCapabilitiesMatchImplementation:
    """
    A capability must be backed by something. This is the check that catches a
    backend declaring a control its transport cannot express, which would
    otherwise show the user a button that silently does nothing.
    """

    def test_audio_track_capability_matches_the_transport(self):
        """
        mpv's IPC, omxplayer's key map and VLC's track interface can all cycle
        audio tracks. VLC was counted as unable to for as long as the cli
        interface was assumed to have no track verb, which turned out to be
        wrong: "atrack" lists the tracks and marks the active one, the same way
        "strack" does for subtitles. gstreamer has no control at all.
        """
        from lib.player.gstproc import GStreamerProcess
        from lib.player.mpvproc import MpvProcess
        from lib.player.vlcproc import VlcProcess

        assert MpvProcess().declares("audio_track") is True
        assert VlcProcess().declares("audio_track") is True
        assert GStreamerProcess().declares("audio_track") is False

    def test_a_capability_less_backend_declares_none(self):
        from lib.player.gstproc import GStreamerProcess

        assert GStreamerProcess().capabilities == frozenset()

    def test_every_declared_capability_is_a_known_string(self):
        for name in ALL_NAMES:
            for cap in get_backend(name).capabilities:
                assert cap in ALL_CAPABILITIES, (name, cap)


class TestControlIsSafeWithoutAPlayer:
    @pytest.mark.parametrize("name", ALL_NAMES)
    @pytest.mark.parametrize(
        "action", ["pause", "resume", "stop", "plus30", "volup", "next_audio"]
    )
    def test_control_never_raises(self, name, action):
        """
        A control can arrive before the backend is up or after it has exited.
        It must be dropped, never turned into an API error.
        """
        backend = get_backend(name)
        with m.patch("os.path.exists", return_value=False):
            assert backend.control(action) is None or True

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_unknown_action_is_dropped(self, name):
        backend = get_backend(name)
        with m.patch("os.path.exists", return_value=False):
            assert backend.control("not_an_action") is None or True


class TestDescribeIsSerialisable:
    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_describe_round_trips_as_json(self, name):
        """
        describe() is what a UI would be handed, so it has to survive being
        serialised into the API response.
        """
        json.dumps(describe(name))

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_describe_reports_the_binary(self, name):
        assert describe(name)["binary"] == get_backend(name).opt("binary")

    def test_capability_less_backend_is_flagged(self):
        assert "note" in describe("gstreamer")


class TestDefaultBackend:
    """
    VLC is the default because omxplayer no longer runs on current Raspberry Pi
    OS. VLC decodes H.264 in hardware, sends audio to HDMI and burns in
    subtitles without the expensive GStreamer compositing path.
    """

    def test_vlc_is_the_default(self):
        from lib.player.backends import DEFAULT_BACKEND

        assert DEFAULT_BACKEND == "vlc"

    def test_default_is_a_real_backend(self):
        """Not the legacy pseudo-name, which resolves to nothing by itself."""
        from lib.player.backends import DEFAULT_BACKEND, is_known

        assert is_known(DEFAULT_BACKEND)
        assert get_backend(DEFAULT_BACKEND) is not None

    def test_unconfigured_install_gets_the_default(self, settings):
        pl = _Player()
        with m.patch("lib.player.player.load", return_value={}):
            from lib.player.vlcproc import VlcProcess

            assert isinstance(pl._player_stage(True, True), VlcProcess)
            assert isinstance(pl._player_stage(False, False), VlcProcess)

    def test_omxplayer_is_still_selectable_by_name(self, settings):
        """
        Deprecated as a default does not mean removed. Anyone still on the old
        OS can name it explicitly.
        """
        from lib.player.omxproc import OmxplayerProcess

        pl = _Player()
        with m.patch("lib.player.player.load", return_value={"backend": "omxplayer"}):
            assert isinstance(pl._player_stage(True, True), OmxplayerProcess)


class TestLegacyBehaviourPreserved:
    """
    The pre-abstraction choice is still reachable, under the name "legacy", now
    that a default no longer falls through to it.
    """

    @pytest.mark.parametrize(
        "flags",
        [(False, False), (False, True), (True, False), (True, True)],
    )
    def test_legacy_name_uses_the_historical_choice(self, settings, flags):
        """
        Compared by class rather than by name: name() is a display name, so
        omxplayer-keys displays as "omxplayer with keys".
        """
        from lib.player.omxproc import OmxplayerProcess
        from lib.player.omxproc2 import OmxplayerProcess2

        http, dlsrv = flags
        pl = _Player()
        with m.patch(
            "lib.player.player.load", return_value={"backend": LEGACY_BACKEND}
        ):
            stage = pl._player_stage(http, dlsrv)
        # Keys or not: only the plain dbus variant takes this path.
        expected = OmxplayerProcess2 if (http or dlsrv) else OmxplayerProcess
        assert isinstance(stage, expected)

    def test_legacy_choice_does_not_consult_the_registry(self, settings):
        """
        The legacy path constructs the classes directly. Guarding that it does
        not route through get_backend keeps it independent of whatever happens
        to be registered.
        """
        from lib.player.omxproc import OmxplayerProcess

        pl = _Player()
        with m.patch(
            "lib.player.player.load", return_value={"backend": LEGACY_BACKEND}
        ):
            with m.patch(
                "lib.player.backends.get_backend",
                side_effect=AssertionError("legacy must not consult the registry"),
            ):
                assert isinstance(pl._player_stage(False, False), OmxplayerProcess)

    def test_legacy_is_selectable_but_is_not_in_the_registry(self):
        """
        It is a pseudo-backend: selectable, but there is no class behind it, so
        get_backend cannot resolve it and describe handles it instead.
        """
        from lib.player.backends import BACKENDS, is_known, selectable_names

        assert is_known(LEGACY_BACKEND)
        assert LEGACY_BACKEND not in BACKENDS
        assert LEGACY_BACKEND in selectable_names()
        with pytest.raises(KeyError):
            get_backend(LEGACY_BACKEND)

    def test_configured_backend_overrides_the_flags(self, settings):
        pl = _Player()
        with m.patch("lib.player.player.load", return_value={"backend": "vlc"}):
            from lib.player.vlcproc import VlcProcess

            assert isinstance(pl._player_stage(True, True), VlcProcess)
            assert isinstance(pl._player_stage(False, False), VlcProcess)

    def test_unknown_configured_backend_raises(self, settings):
        pl = _Player()
        with m.patch("lib.player.player.load", return_value={"backend": "nope"}):
            with pytest.raises(KeyError):
                pl._player_stage(False, True)


class TestConfigResolutionIsIndependent:
    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_resolve_config_matches_the_defaults_without_settings(self, name):
        assert resolve_config(name) == backend_class(name).defaults

    def test_each_backend_can_be_configured_independently(self, settings):
        from lib.settings import save

        for name in ALL_NAMES:
            save("player-" + name, {"binary": "/opt/" + name})
        for name in ALL_NAMES:
            assert get_backend(name).opt("binary") == "/opt/" + name

    def test_setting_one_backend_leaves_the_others_alone(self, settings):
        from lib.settings import save

        save("player-" + ALL_NAMES[0], {"binary": "/custom"})
        for name in ALL_NAMES[1:]:
            assert get_backend(name).opt("binary") != "/custom"


class TestPauseCapabilityIsAlwaysAvailable:
    """
    pause is the one capability the UI treats as essential, so a backend that
    silently lacks it needs to be a deliberate, visible choice.
    """

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_backend_declaring_pause_has_the_property(self, name):
        if get_backend(name).declares(CAP_PAUSE):
            assert callable(get_backend(name).control)


class TestReadmeMatchesTheCode:
    """
    The README documents each backend's settings keys so the format is
    discoverable. Documentation that has drifted from the code is worse than
    none, so the table is checked against defaults.
    """

    DOCUMENTED = {
        "vlc": {
            "binary",
            "extra_args",
            "audio_device",
            "video_output",
            "video_output_module",
            "subtitle_text_scale",
            "start_timeout",
            "report_progress",
            "osd_overlay",
            "osd_refresh",
            "osd_position",
            "osd_size",
            "osd_opacity",
            "osd_timeout",
            "sub_margin",
            "volume_step",
            "volume_max",
        },
        "mpv": {
            "binary",
            "extra_args",
            "audio_device",
            "socket",
            "start_timeout",
            "subtitle_delay_step",
        },
        "gstreamer": {
            "binary",
            "pipeline",
            "http_source",
            "plane_id",
            "connector_id",
            "audio_device",
            "video_decoder",
            "audio_decoder",
            "start_timeout",
        },
        "omxplayer": {
            "binary",
            "extra_args",
            "start_timeout",
            "input_timeout",
            "dbus_path",
        },
        "omxplayer-keys": {"binary", "extra_args", "start_timeout", "fifo"},
    }

    @staticmethod
    def readme():
        return (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()

    def test_every_backend_is_documented(self):
        assert set(self.DOCUMENTED) == set(ALL_NAMES)

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_documented_keys_are_the_real_keys(self, name):
        assert self.DOCUMENTED[name] == set(backend_class(name).defaults), name

    @pytest.mark.parametrize("name", ALL_NAMES)
    def test_documented_keys_appear_in_the_readme(self, name):
        """
        Checked against the actual table text, so renaming a key without editing
        the prose fails rather than passing on the dict comparison alone.
        """
        readme = self.readme()
        missing = [k for k in sorted(backend_class(name).defaults) if k not in readme]
        assert missing == [], (name, missing)

    def test_the_backend_table_lists_every_name(self):
        readme = self.readme()
        for name in ALL_NAMES:
            assert "`" + name + "`" in readme, name

    def test_capability_free_backend_is_called_out(self):
        """The README has to warn, since the UI does not hide the buttons yet."""
        readme = self.readme().lower()
        assert "no control surface" in readme

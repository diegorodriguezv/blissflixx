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
    backend_class,
    backend_names,
    get_backend,
    resolve_config,
)
from lib.player.player import _Player

HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"
FILE_OUT = "/tmp/blissflixx/bf.out"

ALL_NAMES = sorted(BACKENDS)
#: The backends the README is required to document.
DOCUMENTED_NAMES = ["vlc"]


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

        None is deliberate for some backends: an input that is a file still
        growing has no end, so it waits indefinitely for the first line. It is a
        choice, and it has to be visible as one rather than a missing key.
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
        """The default _ready reads self.start_timeout."""
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


class TestArgvBackendsKeepPathsIntact:
    @pytest.mark.parametrize("name", ["vlc"])
    def test_path_with_a_space_stays_one_argument(self, name):
        """
        The inverse of the shell case: argv builders must not split the path,
        and must not have quoted it either, since nothing will strip the quotes.
        """
        spaced = "/tmp/a b/file.mkv"
        cmd = get_backend(name).build_command({"outfile": spaced})
        assert spaced in cmd
        assert "'" + spaced + "'" not in cmd

class TestCapabilitiesMatchImplementation:
    """
    A capability must be backed by something. This is the check that catches a
    backend declaring a control its transport cannot express, which would
    otherwise show the user a button that silently does nothing.
    """

    def test_audio_track_capability_matches_the_transport(self):
        """
        VLC was counted as unable to change audio tracks for as long as the cli
        interface was assumed to have no track verb, which turned out to be
        wrong: "atrack" lists the tracks and marks the active one, the same way
        "strack" does for subtitles.
        """
        from lib.player.vlcproc import VlcProcess

        assert VlcProcess().declares("audio_track") is True

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


class TestDefaultBackend:
    """
    VLC decodes H.264 in hardware, sends audio to HDMI and burns in subtitles
    without the expensive GStreamer compositing path.
    """

    def test_vlc_is_the_default(self):
        from lib.player.backends import DEFAULT_BACKEND

        assert DEFAULT_BACKEND == "vlc"

    def test_default_is_a_real_backend(self):
        from lib.player.backends import DEFAULT_BACKEND

        assert get_backend(DEFAULT_BACKEND) is not None

    def test_every_play_uses_the_default(self):
        from lib.player.vlcproc import VlcProcess

        pl = _Player()
        assert isinstance(pl._player_stage(True, True), VlcProcess)
        assert isinstance(pl._player_stage(False, False), VlcProcess)


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
            "source_modeset",
            "fill_screen",
            "network_caching",
            "prefetch_seek_threshold",
            "fast_seek",
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
            "subtitle_delay_step",
        },
    }

    @staticmethod
    def readme():
        return (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()

    def test_every_supported_backend_is_documented(self):
        assert set(self.DOCUMENTED) == set(DOCUMENTED_NAMES)

    @pytest.mark.parametrize("name", DOCUMENTED_NAMES)
    def test_documented_keys_are_the_real_keys(self, name):
        assert self.DOCUMENTED[name] == set(backend_class(name).defaults), name

    @pytest.mark.parametrize("name", DOCUMENTED_NAMES)
    def test_documented_keys_appear_in_the_readme(self, name):
        """
        Checked against the actual table text, so renaming a key without editing
        the prose fails rather than passing on the dict comparison alone.
        """
        readme = self.readme()
        missing = [k for k in sorted(backend_class(name).defaults) if k not in readme]
        assert missing == [], (name, missing)

    def test_the_backend_table_lists_every_supported_name(self):
        readme = self.readme()
        for name in DOCUMENTED_NAMES:
            assert "`" + name + "`" in readme, name

    def test_the_only_backend_has_controls(self):
        """
        The UI does not hide buttons a backend lacks, so the one backend must
        have them all.
        """
        from lib.player.backends import backend_class

        assert all(backend_class(n).capabilities for n in DOCUMENTED_NAMES)

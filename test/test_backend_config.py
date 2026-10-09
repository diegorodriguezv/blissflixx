"""
Per-backend configuration.

Backends declare defaults in code and merge data/settings/player-<name> over the
top. Two things matter enough to test directly:

- An absent or partial settings file must leave the command exactly as it was
  before configuration existed, so a checkout with no settings behaves as before.
- A settings file must be able to describe a *different machine*. That is the
  point of the layer: an ALSA card name or a KMS plane id differs between a Pi 4,
  a Pi 5 and a desktop, and those are values rather than branches in code.

The autouse temp_settings fixture in conftest.py already redirects SETTINGS_PATH
into a temporary directory and clears lib.settings' cache between tests, so
writing settings here is isolated by construction.
"""

import json

import pytest

from lib.player.backend import PlayerBackend
from lib.player.backends import (
    BACKENDS,
    DEFAULT_BACKEND,
    LEGACY_BACKEND,
    backend_class,
    backend_names,
    describe,
    get_backend,
    resolve_config,
    settings_name,
)
from lib.player.mpvproc import MpvProcess
from lib.player.omxproc import OmxplayerProcess
from lib.player.omxproc2 import OmxplayerProcess2
from lib.settings import save

FILE_OUT = "/tmp/blissflixx/bf.out"
HTTP_OUT = "http://127.0.0.1:9696/movie.mkv"


def write_settings(name, data):
    """Persist a backend settings file the way an operator would."""
    save(settings_name(name), data)


class TestSettingsNaming:
    def test_config_file_is_namespaced_under_player(self):
        assert settings_name("mpv") == "player-mpv"

    def test_no_collision_with_the_active_choice_file(self):
        """
        The active backend lives in data/settings/player, so a backend must not
        be named such that its own file is that one.
        """
        assert settings_name("") != "player"
        for name in backend_names():
            assert settings_name(name) != "player"


class TestDefaultsWithoutSettings:
    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys", "mpv"])
    def test_resolve_config_yields_the_defaults(self, name):
        assert resolve_config(name) == backend_class(name).defaults

    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys", "mpv"])
    def test_get_backend_constructs_from_defaults_alone(self, name):
        assert isinstance(get_backend(name), backend_class(name))

    def test_absent_settings_file_is_not_an_error(self, settings):
        """No file at all is the common case, not a failure."""
        assert resolve_config("mpv")["binary"] == "mpv"


class TestOverrideSemantics:
    def test_single_key_overrides(self, settings):
        write_settings("mpv", {"audio_device": "alsa/plughw:0"})
        assert get_backend("mpv").opt("audio_device") == "alsa/plughw:0"

    def test_unmentioned_keys_keep_their_defaults(self, settings):
        write_settings("mpv", {"audio_device": "alsa/plughw:0"})
        backend = get_backend("mpv")
        assert backend.opt("binary") == MpvProcess.defaults["binary"]

    def test_a_full_override_need_not_repeat_the_defaults(self, settings):
        write_settings(
            "omxplayer-keys",
            {"binary": "/opt/omxplayer/omxplayer.bin"},
        )
        backend = get_backend("omxplayer-keys")
        assert backend.opt("binary") == "/opt/omxplayer/omxplayer.bin"
        assert backend.opt("extra_args") == OmxplayerProcess2.defaults["extra_args"]

    def test_empty_settings_file_is_the_same_as_absent(self, settings):
        write_settings("mpv", {})
        assert resolve_config("mpv") == MpvProcess.defaults

    def test_extra_args_replace_rather_than_append(self, settings):
        """
        Replacement is what makes it possible to *remove* a flag the backend
        always adds. Appending would make --no-config impossible to drop.
        """
        write_settings("mpv", {"extra_args": ["--profile=gpu-hq"]})
        assert get_backend("mpv").opt("extra_args") == ["--profile=gpu-hq"]
        cmd = get_backend("mpv").build_command({"outfile": FILE_OUT})
        assert "--no-config" not in cmd
        assert "--profile=gpu-hq" in cmd


class TestUnknownKeys:
    def test_unknown_key_is_ignored(self, settings):
        """
        A backend must stay usable when a settings file written for a different
        version names something that no longer exists.
        """
        write_settings("mpv", {"audio_device": "alsa/plughw:0", "hwdec": "auto"})
        backend = get_backend("mpv")
        assert backend.opt("audio_device") == "alsa/plughw:0"
        assert "hwdec" not in backend.config

    def test_unknown_key_is_logged(self, settings, caplog):
        """
        Asserted through caplog, which is where cherrypy.log's output ends up.

        An earlier version of this assigned cherrypy.log directly. That replaces
        a module global for the rest of the session, so every test after it saw
        no log output at all — which is how a passing VLC reply-logging test came
        to fail depending on where in the suite it ran.
        """
        with caplog.at_level("INFO"):
            write_settings("mpv", {"hwdec": "auto"})
            get_backend("mpv")
        assert any("hwdec" in record.getMessage() for record in caplog.records)

    def test_typo_in_a_real_key_still_uses_the_default(self, settings):
        """A misspelt key must not silently disable the setting."""
        write_settings("mpv", {"audio_devise": "alsa/plughw:0"})
        assert (
            get_backend("mpv").opt("audio_device")
            == MpvProcess.defaults["audio_device"]
        )


class TestPlatformVariants:
    """
    The reason this layer exists: one checkout, several machines.

    These are table-driven config dicts, which is the point. Before
    configuration, expressing a second invocation meant editing a constant.
    """

    @pytest.mark.parametrize(
        "label,device",
        [
            ("pi4", "alsa/hdmi:CARD=vc4hdmi,DEV=0"),
            ("pi5", "alsa/hdmi:CARD=hdmi_zero,DEV=0"),
            ("desktop", "alsa/plughw:CARD=PCH,DEV=0"),
        ],
    )
    def test_mpv_audio_device_variants(self, settings, label, device):
        write_settings("mpv", {"audio_device": device})
        cmd = get_backend("mpv").build_command({"outfile": FILE_OUT})
        assert "--audio-device=" + device in cmd, label

    def test_two_registered_names_can_share_a_class(self, settings):
        """
        A Pi 4 and a Pi 5 profile of the same player, side by side in one
        checkout, with no platform conditional in the code.
        """
        BACKENDS["mpv-test"] = MpvProcess
        try:
            write_settings("mpv", {"audio_device": "alsa/hdmi:CARD=vc4hdmi,DEV=0"})
            write_settings("mpv-test", {"audio_device": "alsa/plughw:0"})
            assert get_backend("mpv").opt("audio_device") != (
                get_backend("mpv-test").opt("audio_device")
            )
        finally:
            del BACKENDS["mpv-test"]


class TestRegistryInvariants:
    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys", "mpv"])
    def test_every_backend_declares_its_defaults(self, name):
        """A backend with no defaults dict cannot be configured or described."""
        assert isinstance(backend_class(name).defaults, dict)
        assert backend_class(name).defaults, name

    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys", "mpv"])
    def test_every_default_value_is_json_serialisable(self, name):
        """
        Settings are JSON, so a default that cannot round-trip through the file
        format would be a trap.
        """
        json.dumps(backend_class(name).defaults)

    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys", "mpv"])
    def test_every_backend_has_a_binary(self, name):
        """Needed by describe(), and by anything that reports what will run."""
        assert get_backend(name).opt("binary")

    @pytest.mark.parametrize("name", ["omxplayer", "omxplayer-keys", "mpv"])
    def test_every_backend_builds_a_command(self, name):
        cmd = get_backend(name).build_command({"outfile": HTTP_OUT})
        assert cmd

    def test_capabilities_are_a_subset_of_the_known_set(self):
        from lib.player.backend import ALL_CAPABILITIES

        for name in backend_names():
            assert get_backend(name).capabilities <= ALL_CAPABILITIES, name

    def test_unknown_name_raises_rather_than_falling_back(self):
        with pytest.raises(KeyError):
            get_backend("vlc-ish")

    def test_default_backend_is_configured(self):
        """
        Not None any more. omxplayer no longer runs on current Raspberry Pi OS,
        so an unconfigured install resolves to a real backend rather than falling
        through to the legacy omxplayer choice.
        """
        assert DEFAULT_BACKEND is not None
        assert DEFAULT_BACKEND != LEGACY_BACKEND

    def test_describe_reports_what_will_run(self):
        info = describe("mpv")
        assert info["backend"] == "mpv"
        assert info["name"] == "mpv"
        assert info["binary"] == "mpv"
        assert "pause" in info["capabilities"]


class TestConfigIsNotShared:
    def test_mutating_one_instance_does_not_affect_the_defaults(self, settings):
        backend = get_backend("mpv")
        backend.config["audio_device"] = "mutated"
        assert MpvProcess.defaults["audio_device"] != "mutated"

    def test_two_instances_are_independent(self, settings):
        first = get_backend("mpv")
        first.config["binary"] = "changed"
        assert get_backend("mpv").opt("binary") == MpvProcess.defaults["binary"]


class TestBaseMerge:
    def test_base_defaults_are_empty(self):
        assert PlayerBackend.defaults == {}

    def test_opt_requires_a_known_key(self, settings):
        with pytest.raises(KeyError):
            get_backend("mpv").opt("not_a_real_key")

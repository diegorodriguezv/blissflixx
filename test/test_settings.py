"""
Settings persistence.

lib.settings caches loaded values in a module-level dict and reads/writes JSON
files under data/settings. The conftest fixture redirects that to a temporary
directory, so these tests never touch a real install.
"""

import json

from lib import settings as settings_module


class TestLoad:
    def test_missing_file_returns_empty_dict(self, settings):
        assert settings.load("nothing-here") == {}

    def test_missing_file_is_not_cached(self, settings):
        settings.load("nothing-here")
        assert "nothing-here" not in settings._cache

    def test_load_after_save(self, settings):
        settings.save("channels", {"bfch_x": {"disabled": True}})
        settings._cache.clear()
        assert settings.load("channels") == {"bfch_x": {"disabled": True}}

    def test_save_writes_json_to_disk(self, settings, tmp_path):
        settings.save("subtitles", {"lang": "fra"})
        path = tmp_path / "settings" / "subtitles"
        assert json.loads(path.read_text()) == {"lang": "fra"}

    def test_load_reads_hand_edited_file(self, settings, tmp_path):
        path = tmp_path / "settings" / "channels"
        path.write_text(json.dumps({"a": {"disabled": False}}))
        assert settings.load("channels") == {"a": {"disabled": False}}

    def test_cache_avoids_rereading(self, settings, tmp_path):
        settings.save("k", {"v": 1})
        path = tmp_path / "settings" / "k"
        path.unlink()
        # Still served from cache even though the file is gone.
        assert settings.load("k") == {"v": 1}


class TestChannelSettingsUsage:
    """
    The settings module is consumed through lib/api/channels rather than
    directly, so check the shape it relies on.
    """

    def test_disabled_flag_round_trip(self, settings):
        from lib.api.channels import InstalledChannels

        inst = InstalledChannels.__new__(InstalledChannels)
        inst.settings = settings.load("channels")
        inst.disableChannel("bfch_test")
        assert inst.isEnabled("bfch_test") is False
        settings._cache.clear()
        assert settings.load("channels")["bfch_test"]["disabled"] is True

    def test_unknown_channel_is_enabled_by_default(self, settings):
        from lib.api.channels import InstalledChannels

        inst = InstalledChannels.__new__(InstalledChannels)
        inst.settings = {}
        assert inst.isEnabled("bfch_never_seen") is True


class TestSettingsModuleSurface:
    def test_exposes_load_and_save(self):
        assert callable(settings_module.load)
        assert callable(settings_module.save)

    def test_path_uses_redirected_settings_dir(self, settings, tmp_path):
        settings.save("probe", {"x": 1})
        assert (tmp_path / "settings" / "probe").exists()

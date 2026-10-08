"""
Location resolution.

lib/locations exports paths that everything else depends on: the plugin
loader adds CHAN_PATH to sys.path, the player interpolates BIN_PATH into shell
commands, and settings writes under SETTINGS_PATH. A silent off-by-one here
would send the server looking in the wrong directory, or worse, write user data
somewhere unexpected.

These tests pin the values against the expressions the module used to evaluate
rather than against hardcoded paths, so they stay correct if the checkout moves.
"""

import os.path as osp
from pathlib import Path

import pytest

from lib import locations


class TestValuesMatchOriginalExpressions:
    """
    The previous implementation was:

        LIB_PATH = path.split(path.abspath(path.dirname(__file__)))[0]
        ROOT_PATH = path.split(LIB_PATH)[0]
        HTML_PATH = path.join(ROOT_PATH, "html")
        ...

    Re-evaluated here for the real __file__, so any drift shows up.
    """

    @pytest.fixture
    def original(self):
        this_file = osp.abspath(locations.__file__)
        lib = osp.split(osp.abspath(osp.dirname(this_file)))[0]
        root = osp.split(lib)[0]
        data = osp.join(root, "data")
        return {
            "ROOT_PATH": root,
            "LIB_PATH": lib,
            "HTML_PATH": osp.join(root, "html"),
            "YTUBE_PATH": osp.join(lib, "yt-dlp"),
            "DATA_PATH": data,
            "BIN_PATH": osp.join(root, "bin"),
            "PLIST_PATH": osp.join(data, "playlists"),
            "SETTINGS_PATH": osp.join(data, "settings"),
            "CHAN_PATH": osp.join(root, "chls"),
            "PLUGIN_PATH": osp.join(root, "plugins"),
        }

    @pytest.mark.parametrize(
        "name",
        [
            "ROOT_PATH",
            "LIB_PATH",
            "HTML_PATH",
            "YTUBE_PATH",
            "DATA_PATH",
            "BIN_PATH",
            "PLIST_PATH",
            "SETTINGS_PATH",
            "CHAN_PATH",
            "PLUGIN_PATH",
        ],
    )
    def test_value_is_unchanged(self, original, name):
        assert getattr(locations, name) == original[name]

    def test_everything_is_str(self):
        """
        str, not Path: these are interpolated into shell commands by
        lib/player and handed to subprocess.
        """
        for name in dir(locations):
            if name.endswith("_PATH"):
                assert isinstance(getattr(locations, name), str), name


class TestRelationships:
    """
    The structure matters independently of where the checkout lives: settings
    live under data, playlists alongside them, and everything except yt-dlp and
    lib itself hangs off the root.
    """

    def test_root_is_the_checkout(self):
        assert (
            Path(locations.ROOT_PATH).resolve()
            == Path(locations.__file__).resolve().parents[2]
        )

    def test_lib_is_inside_root(self):
        assert osp.dirname(locations.LIB_PATH) == locations.ROOT_PATH

    def test_channels_and_plugins_are_siblings_under_root(self):
        for name in ("CHAN_PATH", "PLUGIN_PATH", "HTML_PATH", "BIN_PATH", "DATA_PATH"):
            assert osp.dirname(getattr(locations, name)) == locations.ROOT_PATH, name

    def test_settings_and_playlists_live_under_data(self):
        for name in ("SETTINGS_PATH", "PLIST_PATH"):
            assert osp.dirname(getattr(locations, name)) == locations.DATA_PATH, name

    def test_yt_dlp_lives_under_lib(self):
        assert osp.dirname(locations.YTUBE_PATH) == locations.LIB_PATH

    def test_expected_directories_exist_in_a_checkout(self):
        for name in ("ROOT_PATH", "LIB_PATH", "CHAN_PATH", "HTML_PATH", "BIN_PATH"):
            assert osp.isdir(getattr(locations, name)), name

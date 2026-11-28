"""
Path resolution.

blissflixx/paths exports the locations everything else depends on: the channel
loader imports the built-in channels from CHAN_PATH and scans PLUGIN_PATH, the
player interpolates BIN_PATH into shell commands, and settings writes under
SETTINGS_PATH. A wrong answer here sends the server looking in the wrong
directory, or writes user data somewhere unexpected.

The root is found by its markers, not by counting parents from the module, and
both the root and the data directory can be named by environment variables. The
constants are evaluated at import, so the tests that change the environment run
a fresh interpreter for the end-to-end case.
"""

import os
import os.path as osp
import pathlib
import subprocess
import sys

import pytest

from blissflixx import paths

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_snippet(code, extra_env=None):
    env = dict(os.environ)
    env.pop("BLISSFLIXX_ROOT", None)
    env.pop("BLISSFLIXX_DATA", None)
    env.update(extra_env or {})
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


def make_checkout(parent):
    root = parent / "checkout"
    (root / "html").mkdir(parents=True)
    (root / "pyproject.toml").write_text("")
    return root


class TestFindingTheRoot:
    def test_found_from_a_directory_inside_the_checkout(self, tmp_path):
        root = make_checkout(tmp_path)
        start = root / "blissflixx" / "player" / "deeper"
        start.mkdir(parents=True)

        assert paths.find_root(start) == root

    def test_a_directory_with_only_half_the_markers_is_not_the_root(self, tmp_path):
        """
        pyproject.toml alone can belong to a workspace above the checkout;
        html/ alone could be any web directory. Both are what make it ours.
        """
        root = make_checkout(tmp_path)
        (root / "html").rmdir()

        with pytest.raises(RuntimeError):
            paths.find_root(root / "blissflixx")

    def test_without_one_it_says_how_to_name_it(self, tmp_path):
        with pytest.raises(RuntimeError, match="BLISSFLIXX_ROOT"):
            paths.find_root(tmp_path)

    def test_the_environment_names_it_explicitly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLISSFLIXX_ROOT", str(tmp_path))

        assert paths.find_root(pathlib.Path("/")) == tmp_path

    def test_the_real_module_finds_this_checkout(self):
        assert pathlib.Path(paths.ROOT_PATH) == paths.find_root()


class TestTheDataDirectory:
    def test_defaults_to_data_under_the_checkout(self, tmp_path):
        assert paths.data_dir(tmp_path) == tmp_path / "data"

    def test_the_environment_moves_it(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLISSFLIXX_DATA", str(tmp_path / "state"))

        assert paths.data_dir(pathlib.Path("/somewhere")) == tmp_path / "state"

    def test_an_empty_value_means_unset(self, tmp_path, monkeypatch):
        monkeypatch.setenv("BLISSFLIXX_DATA", "")

        assert paths.data_dir(tmp_path) == tmp_path / "data"


class TestTheEnvironmentReachesTheConstants:
    def test_the_root_and_data_can_both_be_named(self, tmp_path):
        root = make_checkout(tmp_path)
        state = tmp_path / "state"

        proc = run_snippet(
            "from blissflixx import paths; "
            "print(paths.ROOT_PATH); print(paths.DATA_PATH); "
            "print(paths.SETTINGS_PATH)",
            {"BLISSFLIXX_ROOT": str(root), "BLISSFLIXX_DATA": str(state)},
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [
            str(root),
            str(state),
            str(state / "settings"),
        ]

    def test_data_alone_leaves_the_root_where_it_was(self, tmp_path):
        state = tmp_path / "state"

        proc = run_snippet(
            "from blissflixx import paths; print(paths.ROOT_PATH); "
            "print(paths.DATA_PATH)",
            {"BLISSFLIXX_DATA": str(state)},
        )

        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.splitlines() == [str(REPO_ROOT), str(state)]


class TestValues:
    def test_everything_is_str(self):
        """
        str, not Path: these are interpolated into shell commands by the player
        and handed to subprocess.
        """
        for name in dir(paths):
            if name.endswith("_PATH"):
                assert isinstance(getattr(paths, name), str), name

    def test_the_root_is_the_checkout(self):
        root = pathlib.Path(paths.ROOT_PATH)
        assert (root / "pyproject.toml").is_file()
        assert (root / "html").is_dir()

    def test_the_package_is_inside_the_root(self):
        assert osp.dirname(paths.PACKAGE_PATH) == paths.ROOT_PATH

    def test_the_built_in_channels_live_in_the_package(self):
        assert osp.dirname(paths.CHAN_PATH) == paths.PACKAGE_PATH

    def test_plugins_and_the_rest_are_siblings_under_root(self):
        for name in (
            "PLUGIN_PATH",
            "HTML_PATH",
            "BIN_PATH",
            "DATA_PATH",
            "VENDOR_PATH",
        ):
            assert osp.dirname(getattr(paths, name)) == paths.ROOT_PATH, name

    def test_settings_and_runtime_state_live_under_data(self):
        for name in ("SETTINGS_PATH", "PLIST_PATH", "NODE_PATH", "COOKIES_PATH"):
            assert osp.dirname(getattr(paths, name)) == paths.DATA_PATH, name

    def test_there_is_no_yt_dlp_checkout(self):
        # yt-dlp is a package in the virtualenv, so there is no path to it and
        # nothing should invent one.
        assert not hasattr(paths, "YTUBE_PATH")

    def test_expected_directories_exist_in_a_checkout(self):
        for name in (
            "ROOT_PATH",
            "PACKAGE_PATH",
            "CHAN_PATH",
            "HTML_PATH",
            "BIN_PATH",
            "VENDOR_PATH",
        ):
            assert osp.isdir(getattr(paths, name)), name

    def test_the_vendored_download_server_is_present(self):
        assert osp.isfile(osp.join(paths.VENDOR_PATH, "dlsrv"))


class TestRuntimeStateStaysOutOfTheCheckout:
    def test_the_cookies_file_lives_under_data(self):
        assert paths.COOKIES_PATH.startswith(
            paths.DATA_PATH + osp.sep
        ), paths.COOKIES_PATH

    def test_data_is_ignored(self):
        ignore = (pathlib.Path(paths.ROOT_PATH) / ".gitignore").read_text().splitlines()
        assert "data/" in [line.strip() for line in ignore], ignore

    def test_the_runtime_is_under_data_too(self):
        """
        The JavaScript runtime is unpacked into data/node by configure.sh -- a
        couple of hundred megabytes that must never be a commit either.
        """
        assert paths.NODE_PATH.startswith(paths.DATA_PATH + osp.sep), paths.NODE_PATH

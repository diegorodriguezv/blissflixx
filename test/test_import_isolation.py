"""
Import graph guards.

These have to run in fresh interpreters: import side effects are cached in
sys.modules, so a test that already imported the whole tree cannot tell whether
an import was clean or merely already done. Each check therefore shells out.

This matters because the import graph has broken twice. lib/api/__init__.py used
to eagerly import its submodules, one of which instantiated InstalledChannels()
at import time and re-imported every installed channel. That produced two hard
circular imports:

    import lib.player -> pflixproc -> lib.api -> playr -> lib.player
    import bfch_eztv  -> lib.api.torrent -> channels -> InstalledChannels()
                         -> re-imports bfch_eztv while it is still executing

Both only failed depending on what happened to be imported first, which is the
worst kind of breakage to notice by hand.
"""

import os
import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_snippet(code):
    """
    Execute python in a fresh interpreter rooted at the repo.

    PYTHONPATH carries the repo root and chls/ so installed channels can be
    imported by bare name, matching how lib/api/channels.py loads them at
    runtime.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / "chls"), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )


class TestStandaloneImports:
    """
    Each of these must work as the very first import in a process.
    """

    def test_player_imports_without_lib_api_first(self):
        proc = run_snippet("import lib.player")
        assert proc.returncode == 0, proc.stderr

    def test_api_imports_without_lib_player_first(self):
        proc = run_snippet("import lib.api.torrent")
        assert proc.returncode == 0, proc.stderr

    def test_api_channels_imports(self):
        proc = run_snippet("import lib.api.channels")
        assert proc.returncode == 0, proc.stderr

    def test_api_playr_imports(self):
        proc = run_snippet("import lib.api.playr")
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize(
        "module",
        [
            "bfch_bbc_iplayer",
            "bfch_eztv",
            "bfch_iptv_org",
            "bfch_pirate_bay",
            "bfch_rotten_tomatoes",
            "bfch_techcrunch",
            "bfch_tmz",
            "bfch_twitch",
            "bfch_vimeo",
            "bfch_youtube",
            "bfch_yts_torrents",
        ],
    )
    def test_channel_imports_standalone(self, module):
        """
        Channels are loaded by bare module name at runtime, so each one must be
        importable on its own. The three that reach into lib.api.torrent are
        the ones the eager hub used to break.
        """
        proc = run_snippet(f"import {module}")
        assert proc.returncode == 0, proc.stderr


class TestImportIsolation:
    def test_api_package_is_an_empty_namespace(self):
        """
        lib/api/__init__.py must not import its submodules. If it does, any
        module importing lib.api.anything drags in cherrypy, the player and the
        channel loader, and the circular imports return.
        """
        proc = run_snippet(
            "import sys, lib.api; "
            "leaked = [m for m in ('cherrypy', 'lib.player', 'lib.api.channels') "
            "if m in sys.modules]; "
            "print(leaked)"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]"

    def test_torrent_module_has_no_web_dependencies(self):
        """
        lib/api/torrent.py is imported by lib/player and by channels, so it has
        to stay a leaf: no cherrypy, no player, no channel loader.
        """
        proc = run_snippet(
            "import sys, lib.api.torrent; "
            "leaked = [m for m in ('cherrypy', 'lib.player', 'lib.api.channels') "
            "if m in sys.modules]; "
            "print(leaked)"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]"

    def test_importing_channels_does_not_load_channels(self):
        """
        InstalledChannels must be created on first use, not at import. Building
        it calls every installed channel's feedlist(), so doing it at import
        made the module unusable from a script or a test and blocked startup.
        """
        proc = run_snippet(
            "import lib.api.channels as c; print(c._installed); "
            "print('bfch_eztv' in __import__('sys').modules)"
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert lines[0] == "None"
        assert lines[1] == "False"

    def test_importing_channels_is_fast(self):
        """
        Importing lib.api.channels must not perform network I/O. This is a
        loose bound rather than a strict time limit, but it catches a
        reintroduced eager InstalledChannels() immediately.
        """
        proc = run_snippet(
            "import time; t = time.time(); import lib.api.channels; "
            "print(time.time() - t)"
        )
        assert proc.returncode == 0, proc.stderr
        assert float(proc.stdout.strip()) < 5.0


class TestEntrypointIsolation:
    def test_importing_the_server_has_no_side_effects(self):
        """
        blissflixx.py used to end in engine.block(), so importing it hung. It
        also cloned yt-dlp over the network and SIGTERMed processes by name.
        All of that now lives in main() behind a __main__ guard.
        """
        proc = run_snippet(
            "import cherrypy, blissflixx; print(bool(cherrypy.tree.apps))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

    def test_main_is_exposed_without_being_called(self):
        proc = run_snippet(
            "import blissflixx; "
            "print(callable(blissflixx.main), callable(blissflixx.mount_trees))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "True True"

    def test_argv_is_not_parsed_at_import(self):
        """
        argparse used to run at import, so importing the module with an
        unexpected sys.argv, as pytest does, could exit the process. parse_args
        is now only called from main().
        """
        proc = run_snippet(
            "import sys; sys.argv = ['pytest', '--some-unknown-flag']; "
            "import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('bf', r'{REPO_ROOT / 'blissflixx.py'}'); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            "print('imported')"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "imported"

    def test_parse_args_returns_defaults_without_a_port(self):
        proc = run_snippet(
            "import blissflixx; "
            "print(blissflixx.parse_args([]).port, blissflixx.parse_args(['--port','8080']).port)"
        )
        assert proc.returncode == 0, proc.stderr
        # The 6969 default is applied in main(), not baked into the parser.
        assert proc.stdout.strip() == "None 8080"


class TestVendoredTreesUntouched:
    def test_yt_dlp_is_not_collected_or_imported_by_the_suite(self):
        proc = run_snippet(
            "import sys, lib.api.torrent; "
            "print(any(m.startswith('yt_dlp') for m in sys.modules))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

    def test_orphan_extractor_package_stays_inert(self):
        """
        lib/extractor has zero importers; it was left commented out rather than
        deleted. Asserting it stays inert guards against someone wiring it back
        into the import graph.
        """
        proc = run_snippet(
            "import lib.extractor, lib.extractor.itv as itv; "
            "print(hasattr(itv, '_get_playlist'))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

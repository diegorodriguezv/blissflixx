"""
Import graph guards.

These have to run in fresh interpreters: import side effects are cached in
sys.modules, so a test that already imported the whole tree cannot tell whether
an import was clean or merely already done. Each check therefore shells out.

This matters because the import graph has broken twice. blissflixx/api/__init__.py used
to eagerly import its submodules, one of which instantiated InstalledChannels()
at import time and re-imported every installed channel. That produced two hard
circular imports:

    import blissflixx.player -> the torrent stage -> blissflixx.api -> playr -> blissflixx.player
    import blissflixx.channels.bfch_eztv -> blissflixx.api.torrent -> channels
                         -> InstalledChannels() -> re-imports it while it is
                         still executing

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

    PYTHONPATH carries the repo root, which is enough: channels that ship with
    the application are packages under blissflixx.channels.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
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

    def test_player_imports_without_api_first(self):
        proc = run_snippet("import blissflixx.player")
        assert proc.returncode == 0, proc.stderr

    def test_api_imports_without_player_first(self):
        proc = run_snippet("import blissflixx.api.torrent")
        assert proc.returncode == 0, proc.stderr

    def test_api_channels_imports(self):
        proc = run_snippet("import blissflixx.api.channels")
        assert proc.returncode == 0, proc.stderr

    def test_api_playr_imports(self):
        proc = run_snippet("import blissflixx.api.playr")
        assert proc.returncode == 0, proc.stderr

    @pytest.mark.parametrize(
        "module",
        [
            "bfch_bbc_iplayer",
            "bfch_divxtotal",
            "bfch_elitetorrent",
            "bfch_eztv",
            "bfch_iptv_org",
            "bfch_limetorrents",
            "bfch_pirate_bay",
            "bfch_rotten_tomatoes",
            "bfch_techcrunch",
            "bfch_therarbg",
            "bfch_tmz",
            "bfch_twitch",
            "bfch_youtube",
            "bfch_yts_torrents",
        ],
    )
    def test_channel_imports_standalone(self, module):
        """
        Each channel is a package under blissflixx.channels, so each one must be
        importable on its own. The three that reach into blissflixx.api.torrent
        are the ones the eager hub used to break.
        """
        proc = run_snippet(f"import blissflixx.channels.{module}")
        assert proc.returncode == 0, proc.stderr


class TestImportIsolation:
    def test_api_package_is_an_empty_namespace(self):
        """
        blissflixx/api/__init__.py must not import its submodules. If it does, any
        module importing blissflixx.api.anything drags in cherrypy, the player and the
        channel loader, and the circular imports return.
        """
        proc = run_snippet(
            "import sys, blissflixx.api; "
            "leaked = [m for m in ('cherrypy', 'blissflixx.player', 'blissflixx.api.channels') "
            "if m in sys.modules]; "
            "print(leaked)"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "[]"

    def test_torrent_module_has_no_web_dependencies(self):
        """
        blissflixx/api/torrent.py is imported by blissflixx/player and by channels, so it has
        to stay a leaf: no cherrypy, no player, no channel loader.
        """
        proc = run_snippet(
            "import sys, blissflixx.api.torrent; "
            "leaked = [m for m in ('cherrypy', 'blissflixx.player', 'blissflixx.api.channels') "
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
            "import blissflixx.api.channels as c; print(c._installed); "
            "print('blissflixx.channels.bfch_eztv' in __import__('sys').modules)"
        )
        assert proc.returncode == 0, proc.stderr
        lines = proc.stdout.strip().splitlines()
        assert lines[0] == "None"
        assert lines[1] == "False"

    def test_importing_channels_is_fast(self):
        """
        Importing blissflixx.api.channels must not perform network I/O. This is a
        loose bound rather than a strict time limit, but it catches a
        reintroduced eager InstalledChannels() immediately.
        """
        proc = run_snippet(
            "import time; t = time.time(); import blissflixx.api.channels; "
            "print(time.time() - t)"
        )
        assert proc.returncode == 0, proc.stderr
        assert float(proc.stdout.strip()) < 5.0


class TestEntrypointIsolation:
    def test_importing_the_server_has_no_side_effects(self):
        """
        blissflixx/server.py used to end in engine.block(), so importing it hung. It
        also cloned yt-dlp over the network and SIGTERMed processes by name.
        All of that now lives in main() behind a __main__ guard.
        """
        proc = run_snippet(
            "import cherrypy, blissflixx.server; print(bool(cherrypy.tree.apps))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

    def test_main_is_exposed_without_being_called(self):
        proc = run_snippet(
            "import blissflixx.server as blissflixx; "
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
            f"spec = importlib.util.spec_from_file_location('bf', r'{REPO_ROOT / 'blissflixx' / 'server.py'}'); "
            "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
            "print('imported')"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "imported"

    def test_parse_args_returns_defaults_without_a_port(self):
        proc = run_snippet(
            "import blissflixx.server as blissflixx; "
            "print(blissflixx.parse_args([]).port, blissflixx.parse_args(['--port','8080']).port)"
        )
        assert proc.returncode == 0, proc.stderr
        # The 6969 default is applied in main(), not baked into the parser.
        assert proc.stdout.strip() == "None 8080"


class TestVendoredTreesUntouched:
    def test_yt_dlp_is_not_collected_or_imported_by_the_suite(self):
        proc = run_snippet(
            "import sys, blissflixx.api.torrent; "
            "print(any(m.startswith('yt_dlp') for m in sys.modules))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"

    def test_orphan_extractor_package_stays_inert(self):
        """
        blissflixx/extractor has zero importers; it was left commented out rather than
        deleted. Asserting it stays inert guards against someone wiring it back
        into the import graph.
        """
        proc = run_snippet(
            "import blissflixx.extractor, blissflixx.extractor.itv as itv; "
            "print(hasattr(itv, '_get_playlist'))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False"


class TestRetiredProcessModules:
    """
    RtmpProcess and LivestreamerProcess were reachable only from wrappers that
    nothing called, and neither livestreamer nor rtmpdump is installed by
    configure.sh. The modules are gone now: importing one must fail, so no
    dependency on an unprovisioned tool can creep back in.
    """

    def test_the_rtmp_module_is_gone(self):
        proc = run_snippet("import blissflixx.player.rtmpproc")
        assert proc.returncode != 0
        assert "ModuleNotFoundError" in proc.stderr

    def test_the_livestreamer_module_is_gone(self):
        proc = run_snippet("import blissflixx.player.lvstrmrproc")
        assert proc.returncode != 0
        assert "ModuleNotFoundError" in proc.stderr

    def test_player_exposes_neither_wrapper(self):
        proc = run_snippet(
            "import blissflixx.player as p; "
            "print(hasattr(p.Player, 'playRtmpdump'), "
            "hasattr(p.Player, 'playLivestream'))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "False False"

    def test_live_playback_paths_are_still_present(self):
        """
        The rest of the player must be untouched by retiring those two. These
        are the methods blissflixx/api/playr.py dispatches to: a file:// url goes to
        playLocalFile, a torrent to playTorrent, everything else to playYtdl.
        Stopping is an action through control(), not a method of its own.
        """
        proc = run_snippet(
            "import blissflixx.player as p; "
            "print(all(hasattr(p.Player, n) for n in "
            "('play', 'playYtdl', 'playTorrent', 'playLocalFile', "
            "'control', 'status', 'quit')))"
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "True"

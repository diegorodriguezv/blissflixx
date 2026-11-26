"""
The compatibility alias that keeps third-party channels importable.

Channels installed under `plugins/` are separate repositories and still say
`from lib.chanutils import ...`; they update on their own schedule. The alias
has to resolve to the *same* module objects, not second copies: the player
singleton, the settings cache and the channel registry are module-level state.
"""

import os
import pathlib
import subprocess
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def run_snippet(code):
    """Run code in a fresh interpreter, where nothing is imported yet."""
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


def output(code):
    proc = run_snippet(code)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


class TestSameModuleObjects:
    def test_the_old_package_name_is_the_package(self):
        assert output("import lib, blissflixx; print(lib is blissflixx)") == "True"

    def test_submodules_are_shared(self):
        code = (
            "import lib.player, blissflixx.player;"
            "import lib.player.player, blissflixx.player.player;"
            "print(lib.player is blissflixx.player,"
            " lib.player.player is blissflixx.player.player)"
        )
        assert output(code) == "True True"

    def test_importing_a_submodule_attaches_it_to_the_package(self):
        code = (
            "import lib.chanutils.reddit, blissflixx;"
            "print(lib.chanutils.reddit is blissflixx.chanutils.reddit)"
        )
        assert output(code) == "True"

    def test_the_shared_module_keeps_its_real_name(self):
        """
        The import machinery stamps `lib.*` metadata on the module it is handed.
        That must be undone, or the real module would start reporting a name
        and package that do not exist.
        """
        code = (
            "import lib.player.player as aliased,"
            " blissflixx.player.player as real;"
            "print(aliased.__name__, aliased.__package__, aliased is real)"
        )
        assert output(code) == ("blissflixx.player.player blissflixx.player True")


class TestTheImportsChannelsActuallyMake:
    def test_the_whole_set_a_channel_uses(self):
        code = (
            "from lib.chanutils import get_attr, get_doc, get_text,"
            " select_all, select_one;"
            "from lib.playitem import PlayItem, PlayItemList;"
            "from lib.api.torrent import TorrentPlayItem;"
            "print('ok')"
        )
        assert output(code) == "ok"


class TestTheAliasDoesNotHideMistakes:
    def test_a_missing_module_still_fails_under_the_old_name(self):
        proc = run_snippet("import lib.player.nothing_here")
        assert proc.returncode != 0
        assert "ModuleNotFoundError" in proc.stderr
        assert "lib.player.nothing_here" in proc.stderr

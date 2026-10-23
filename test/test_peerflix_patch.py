"""
The peerflix patch script.

bin/patch_peerflix.py edits the installed npm tree in place, under one marker
per idea, so a second run is a no-op. These tests run the real script against
a fake tree: they pin the patch machinery (every marker applied, second run
silent, a miss leaves the file alone), not the npm upstream. The loop body
below is verbatim torrent-stream 1.2.1, the version on the boxes; if upstream
ever changes that text the script refuses rather than half-patches, and the
refusal test is what guards it.
"""

import importlib.util
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "patch_peerflix.py"

#: The verification loop body, verbatim torrent-stream 1.2.1. The script's
#: anchors only cover the loop's head; the cache patch spans head to loop(0),
#: so the fake tree needs the whole thing to exercise the full flow.
LOOP_BODY = """      engine.store.get(i, function (_, buf) {
        if (!buf || sha1(buf) !== torrent.pieces[i] || !pieces[i]) return loop(i + 1)
        pieces[i] = null
        engine.bitfield.set(i, true)
        engine.emit('verify', i)
        loop(i + 1)
      })
    }

    loop(0)
"""


def load_script(monkeypatch, tree):
    """Import the script fresh with BF_PEERFLIX_TREE pointed at the fake tree."""
    monkeypatch.setenv("BF_PEERFLIX_TREE", str(tree))
    spec = importlib.util.spec_from_file_location("patch_peerflix_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def make_tree(root, tstream, peerflix="", app=""):
    ts = root / "node_modules" / "torrent-stream"
    ts.mkdir(parents=True)
    (ts / "index.js").write_text(tstream)
    (root / "index.js").write_text(peerflix)
    (root / "app.js").write_text(app)
    return root


def pristine_tree(mod, root):
    """A fake tree shaped like the unpatched npm install."""
    return make_tree(
        root,
        mod.FILES_END + mod.LATE_EMIT + mod.VERIFY_LOOP + LOOP_BODY,
        mod.SETUP_ON_READY + mod.LISTEN_ON_READY,
        mod.STOP_VERIFYING,
    )


class TestPatchScript:
    def test_all_three_markers_applied(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        tstream = (tmp_path / "node_modules" / "torrent-stream" / "index.js").read_text()
        assert mod.MARK in tstream
        assert mod.SWARM_MARK in tstream
        assert mod.CACHE_MARK in tstream
        assert (tmp_path / "index.js").read_text().count(mod.MARK) == 2
        assert mod.SWARM_MARK in (tmp_path / "app.js").read_text()

    def test_second_run_is_a_noop(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        after_first = {
            p: p.read_text()
            for p in [
                tmp_path / "node_modules" / "torrent-stream" / "index.js",
                tmp_path / "index.js",
                tmp_path / "app.js",
            ]
        }
        assert mod.main() == 0
        for p, text in after_first.items():
            assert p.read_text() == text

    def test_cache_hit_signal_present(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        tstream = (tmp_path / "node_modules" / "torrent-stream" / "index.js").read_text()
        assert "bf-verify-cache: hit" in tstream
        assert "BF_VERIFY_CACHE" in tstream

    def test_anchor_miss_leaves_file_untouched(self, tmp_path, monkeypatch, capsys):
        mod = load_script(monkeypatch, tmp_path)
        make_tree(tmp_path, "// some other torrent-stream\n")
        assert mod.main() == 0
        kept = (tmp_path / "node_modules" / "torrent-stream" / "index.js").read_text()
        assert kept == "// some other torrent-stream\n"
        assert mod.CACHE_MARK not in kept

    def test_missing_tree_returns_one(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path / "absent")
        assert mod.main() == 1

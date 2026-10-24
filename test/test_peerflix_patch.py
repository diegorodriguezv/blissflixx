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


def make_tree(root, tstream, peerflix="", app="", fstream=""):
    ts = root / "node_modules" / "torrent-stream"
    ts.mkdir(parents=True)
    (ts / "index.js").write_text(tstream)
    lib = ts / "lib"
    lib.mkdir(exist_ok=True)
    (lib / "file-stream.js").write_text(fstream)
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
        mod.FSTREAM_CTOR + mod.FSTREAM_PARK + mod.FSTREAM_DESTROY,
    )


#: The fixed hunks, mirroring VERIFY_CACHED_LOOP_NEW. If the script changes
#: them without updating these, the upgrade tests below fail -- that is the
#: point: fresh and upgraded trees must converge byte for byte.
FIXED_LOAD_CHECK = """      for (i = 0; i < torrent.files.length; i++) {
        var st
        try {
          st = fs.statSync(path.join(opts.path, torrent.files[i].path))
        } catch (e) { st = null }
        var actual = (st && st.isFile()) ? st.size : -1
        if (actual !== cached.files[i].size) return false
      }
"""

FIXED_SAVE_MAP = """          files: torrent.files.map(function (f) {
            return {
              path: f.path,
              size: bfVerifyCacheSize(f.path)
            }
          })
"""

FIXED_SIZE_DEF = """    // blissflixx:verify-cache-tolerates-missing -- save/load tolerate files
    // that are not on disk (a partial torrent): absent records -1, so the
    // flag is written and refused on presence rather than crashing the save.
    var bfVerifyCacheSize = function (rel) {
      try {
        return fs.statSync(path.join(opts.path, rel)).size
      } catch (e) { return -1 }
    }

    var bfVerifyCacheSave = function () {
"""


def rewind_to_buggy(mod, root):
    """Put the fake tree back to the first-deployed cache text."""
    p = root / "node_modules" / "torrent-stream" / "index.js"
    text = p.read_text()
    text = text.replace(FIXED_LOAD_CHECK, mod.BUGGY_LOAD_CHECK)
    text = text.replace(FIXED_SAVE_MAP, mod.BUGGY_SAVE_SIZES)
    text = text.replace(FIXED_SIZE_DEF, mod.BUGGY_SAVE_DEF)
    assert mod.BUGGY_LOAD_CHECK in text
    assert mod.BUGGY_SAVE_SIZES in text
    assert mod.BUGGY_SAVE_DEF in text
    assert "bfVerifyCacheSize" not in text
    p.write_text(text)
    return p


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
        fstream = (
            tmp_path / "node_modules" / "torrent-stream" / "lib" / "file-stream.js"
        ).read_text()
        assert mod.WAKE_MARK in fstream
        assert "_onverify" in fstream
        assert mod.PARKED_MARK in fstream
        assert "_promote()" in fstream

    def test_second_run_is_a_noop(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        after_first = {
            p: p.read_text()
            for p in [
                tmp_path / "node_modules" / "torrent-stream" / "index.js",
                tmp_path / "node_modules" / "torrent-stream" / "lib" / "file-stream.js",
                tmp_path / "index.js",
                tmp_path / "app.js",
            ]
        }
        assert mod.main() == 0
        for p, text in after_first.items():
            assert p.read_text() == text

    def test_fresh_tree_skips_upgrade(self, tmp_path, monkeypatch, capsys):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        out = capsys.readouterr().out
        assert "not patching" not in out
        assert "tolerate missing" not in out
        tstream = (tmp_path / "node_modules" / "torrent-stream" / "index.js").read_text()
        assert "bfVerifyCacheSize" in tstream

    def test_upgrade_fixes_buggy_tree(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        p = rewind_to_buggy(mod, tmp_path)
        mod2 = load_script(monkeypatch, tmp_path)
        assert mod2.main() == 0
        fixed = p.read_text()
        assert mod2.CACHE_FIX_MARK in fixed
        assert "bfVerifyCacheSize" in fixed
        assert mod2.BUGGY_LOAD_CHECK not in fixed
        assert mod2.BUGGY_SAVE_SIZES not in fixed

    def test_fresh_and_upgraded_converge(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        p = tmp_path / "node_modules" / "torrent-stream" / "index.js"
        fresh = p.read_text()
        rewind_to_buggy(mod, tmp_path)
        mod2 = load_script(monkeypatch, tmp_path)
        assert mod2.main() == 0
        assert p.read_text() == fresh

    def test_cache_hit_signal_present(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path)
        pristine_tree(mod, tmp_path)
        assert mod.main() == 0
        tstream = (tmp_path / "node_modules" / "torrent-stream" / "index.js").read_text()
        assert "bf-verify-cache: hit" in tstream
        assert "BF_VERIFY_CACHE" in tstream

    def test_anchor_miss_leaves_file_untouched(self, tmp_path, monkeypatch, capsys):
        mod = load_script(monkeypatch, tmp_path)
        make_tree(tmp_path, "// some other torrent-stream\n", fstream="// other\n")
        assert mod.main() == 0
        kept = (tmp_path / "node_modules" / "torrent-stream" / "index.js").read_text()
        assert kept == "// some other torrent-stream\n"
        assert mod.CACHE_MARK not in kept
        kept_fs = (
            tmp_path / "node_modules" / "torrent-stream" / "lib" / "file-stream.js"
        ).read_text()
        assert kept_fs == "// other\n"
        assert mod.WAKE_MARK not in kept_fs

    def test_missing_tree_returns_one(self, tmp_path, monkeypatch):
        mod = load_script(monkeypatch, tmp_path / "absent")
        assert mod.main() == 1

"""
The position bar.

Hidden until the film's name is clicked, and while it is hidden the interface
is what it always was. A film only: a live stream has no length to place a
click in, and a film still loading is not anywhere yet.

The behaviour is driven by test/js/playbar_pbar_harness.js, which compiles the
real tag with the same riot and DOM shim the pill harness uses. Skipped where
node is not installed.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "js" / "playbar_pbar_harness.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed here"
)


def _tag():
    root = Path(__file__).resolve().parents[1]
    return (root / "html" / "tags" / "playbar.html").read_text()


def test_the_bar_hides_shows_and_seeks():
    proc = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_the_bar_starts_hidden_and_is_shown_by_the_name():
    """The name is the only way in, and a new play starts from hidden."""
    tag = _tag()
    assert 'show={ pbar }' in tag
    assert '<span class="now-playing-title" onclick={ togglePbar }>' in tag
    assert "self.pbarOn = false" in tag


def test_the_marker_is_written_to_the_element_not_the_template():
    """
    The marker moves four times a second. A style expression would re-render
    the whole tag for each of those; the element is written to instead.
    """
    tag = _tag()
    assert "querySelector('.pbar-marker')" in tag
    assert "style.left = pct + '%'" in tag


def test_a_click_is_an_absolute_seek():
    tag = _tag()
    assert "playerStore.control('seek:' + Math.round(fraction * length)" in tag


def _css():
    root = Path(__file__).resolve().parents[1]
    return (root / "html" / "css" / "layout.css").read_text()


def test_the_top_fifth_is_margin_not_a_click_target():
    """
    A click that misses high is swallowed by the strip rather than landing on
    the page behind it, where it would open whatever it landed on. The
    marker's top edge is the clickable zone's, so where the red stands is
    where to aim.
    """
    tag = _tag()
    assert '<div show={ pbar } class="pbar">' in tag
    assert '<div class="pbar-hit" onclick={ seekTo }>' in tag
    css = _css()
    hit = css[css.index(".pbar-hit"): css.index("}", css.index(".pbar-hit")) + 1]
    assert "top: 20%" in hit
    # The marker lives inside the hit, aligned with it top and bottom.
    assert tag.index('class="pbar-hit"') < tag.index('class="pbar-marker"')

"""
The pill beside the title.

It is the loading count, then the playback clock, and it is also the only way
into the diagnostics panel -- so it has to be there whenever something is
playing. VLC's http interface answers with a position only some of the time,
and for a live stream most of the time it does not answer at all. The bar used
to treat a missing reading as a player with no position, and hid the pill: for a
second at a time on a live stream, and for good once the loading count's five
seconds were up.

The checks live in test/js/playbar_pill_harness.js, which compiles the real tag
with the same riot and DOM shim the compile check uses and drives updateStatus
with status objects shaped like the server's. Skipped where node is not
installed, which is why it is a skip and not an import error.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parent / "js" / "playbar_pill_harness.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed here"
)


def test_the_pill_is_never_hidden_while_something_plays():
    proc = subprocess.run(
        ["node", str(HARNESS)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

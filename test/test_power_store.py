"""
The reboot and shutdown message in the bottom bar.

html/js/PowerStore.js decides when the bar says "Rebooting..." and when it
stops. Almost all of it is a question of sequence -- press, then the server goes
away, then it comes back -- and every one of those orderings was wrong at least
once while it was being written. A PowerStore that cleared the message the
moment the first poll succeeded would have looked fine for about a second and
then been empty for the whole reboot, because systemctl returns as soon as the
job is queued and the machine is still up.

There is no JavaScript test runner in this project, so the checks live in
test/js/power_store_harness.js as a plain node script driven from here. Skipped
where node is not installed, which is why it is a skip and not an import error.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).resolve().parent / "js"
HARNESS = JS / "power_store_harness.js"
TAGS = JS / "riot_tags_check.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed here"
)


def _run(script=HARNESS):
    proc = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, timeout=120
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_the_state_machine_holds():
    """
    Runs every sequence: pressed and nothing yet; the server answering straight
    away because the machine has not gone down yet; gone and back; gone for
    longer than the give-up timer; nothing happening at all; a shutdown that
    does not come back; a reply that never arrives; a server that says no; and
    two presses in a row.
    """
    code, output = _run()
    assert code == 0, output


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed here")
@pytest.mark.parametrize(
    "case",
    [
        "message appears at once",
        "message stays up",
        "message clears when it is back",
        "still saying rebooting",
        "message clears rather than sticking",
        "settles on shut down",
        "treated as the button working",
        "error is shown, not swallowed",
        "only one timer running",
    ],
)
def test_each_case_is_actually_checked(case):
    """
    Guards the harness rather than the store: a sequence silently dropped from
    it would leave the suite green with that behaviour untested, which is how
    two of these were wrong at once in the first place.
    """
    _, output = _run()
    assert case in output, output

def test_the_riot_tags_compile():
    """
    The bar is a riot tag, and a riot tag that will not work fails silently --
    the element simply does not appear, which on a television looks exactly
    like a Pi that never rebooted.

    Also checks for an unclosed expression, which is the failure riot does not
    catch: it renders the text "{ opts.message" instead of the value, and still
    compiles and still registers.
    """
    code, output = _run(TAGS)
    assert code == 0, output

"""
The reboot and shutdown message in the bottom bar.

html/js/PowerStore.js decides what that bar says and when it stops. Almost all
of it is a question of sequence -- press, then the machine stops answering, then
it answers again -- and every one of those orderings was wrong at least once
while it was being written. A store that cleared the message on the first
successful poll would look fine for a second and then be empty for the whole
reboot, because the page polls status every second and the machine is still up
for a moment after it has been asked to go down.

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
        "says it is rebooting",
        "says it is shutting down",
        "the message stays",
        "gone, the moment it answers again",
        "and gone on the first answer back",
        "and stays, because nothing came back",
        "treated as the button working",
        "the message is taken down",
        "and the error is shown",
        "nothing was left armed",
        "and it clears on the way back",
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


class TestARebootDoesNotBlameTheServer:
    """
    What the screen does while the machine is on its way down.

    Something polls the server every second and something routes to the error
    page when a poll fails, so a second after REBOOT is pressed the whole page
    is replaced by "It looks like the server is not running. Please restart the
    server and try again." -- on the screen of someone who just asked it to
    reboot, over the top of the bar saying what is actually happening.

    It is a page in the interface, so it is checked here alongside the rest.
    """

    def test_showError_stays_quiet_while_a_power_action_is_in_flight(self):
        import pathlib
        import subprocess

        from lib import locations

        source = (pathlib.Path(locations.HTML_PATH) / "js" / "Utils.js").read_text()
        assert "powerStore.getMsg()" in source, (
            "Utils.showError still hijacks the screen during a deliberate "
            "reboot or shutdown"
        )

    def test_and_the_check_is_run_by_node_not_by_reading_it(self):
        """
        Grepping the source would pass on a line in a comment, which is how this
        started. This runs the function.
        """
        import json
        import os
        import pathlib
        import shutil
        import subprocess
        import tempfile

        if shutil.which("node") is None:
            pytest.skip("node is not installed here")

        script = """
        var shown = [];
        function riot() {}
        riot.route = function (p) { shown.push(p); };
        var powerMsg = %s;
        var powerStore = powerMsg ? { getMsg: function () { return powerMsg; } } : undefined;
        %s
        Utils.showError('/offline/offline');
        console.log(JSON.stringify(shown));
        """

        from lib import locations

        utils = (pathlib.Path(locations.HTML_PATH) / "js" / "Utils.js").read_text()

        def run(power_msg):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
                fh.write(script % (json.dumps(power_msg), utils))
                path = fh.name
            try:
                proc = subprocess.run(
                    ["node", path], capture_output=True, text=True, timeout=60
                )
                assert proc.returncode == 0, proc.stderr
                return json.loads(proc.stdout.strip())
            finally:
                os.unlink(path)

        # Nothing while a power action is in flight: the bar is saying what is
        # happening and a page about a fault that does not exist is worse than
        # nothing.
        assert run("Rebooting...") == [], "showError still navigated during a reboot"
        # And still everything when there is not, or real errors would be silent.
        assert run(None) == ["error/offline/offline"], (
            "showError stopped reporting faults that are real"
        )

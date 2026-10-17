"""Telling every tab what the machine is about to do to itself.

The message used to be built in the browser, by the tab whose button was
pressed. Every other tab on the same machine showed nothing at all, which is
the same silence as the button having done nothing.

So the notice is sent instead, in the status poll every tab already does once a
second -- the way the restart button's own notice has been sent all along -- and
the machine is given a few seconds before it is asked to go, because that poll
is the only road the notice has and the machine stopping is what closes it.
"""

import pathlib
import re
import unittest.mock as m

import pytest

import blissflixx
from lib import locations


@pytest.fixture
def api():
    return blissflixx.Api()


@pytest.fixture(autouse=True)
def _nothing_left_over(monkeypatch):
    """The notice is a module global and a successful press leaves it set."""
    monkeypatch.setattr(blissflixx.time, "sleep", lambda seconds: None)
    yield
    blissflixx.POWER_MESSAGE = None


def ok(returncode=0, stderr=""):
    return m.Mock(returncode=returncode, stdout="", stderr=stderr)


def status_of(api, **kwargs):
    """One status poll, as a tab makes it."""
    import cherrypy

    cherrypy.response.status = 200
    return api.default("playr", fn="status", **kwargs)


class TestTheOrderOfEvents:
    """
    The notice has to be on its way before the machine stops answering, or there
    is no road for it to travel along. This watches the order rather than the
    clock, because a test that waits three real seconds proves less and takes
    three times as long.
    """

    def test_the_notice_goes_up_before_the_machine_is_asked_to_go(self, api, monkeypatch):
        events = []

        monkeypatch.setattr(
            blissflixx.time,
            "sleep",
            lambda seconds: events.append(("waited", seconds, blissflixx.POWER_MESSAGE)),
        )
        monkeypatch.setattr(
            blissflixx.subprocess,
            "run",
            lambda cmd, **kw: events.append(("asked", cmd)) or ok(),
        )

        api.default("server", fn="reboot")

        assert [event[0] for event in events] == ["waited", "asked"], events
        # And the notice was already up when the wait began, which is the part
        # that matters: it is what the wait exists to let a poll find.
        assert events[0][2] == "Rebooting...", events

    def test_a_shutdown_says_so_and_not_that_it_is_rebooting(self, api, monkeypatch):
        seen = {}

        monkeypatch.setattr(
            blissflixx.time,
            "sleep",
            lambda seconds: seen.update(msg=blissflixx.POWER_MESSAGE),
        )
        monkeypatch.setattr(blissflixx.subprocess, "run", lambda cmd, **kw: ok())

        api.default("server", fn="shutdown")

        assert seen["msg"] == "Shutting down...", seen

    def test_the_wait_is_long_enough_for_a_poll_to_land(self):
        """
        Every tab asks once a second, so anything less than a second can be
        missed by a tab whose poll has just finished -- which is most of them, at
        some moment. And it cannot be long: the point of the button is that the
        machine goes, and a shutdown never comes back to be waited for.
        """
        assert blissflixx.POWER_GRACE_SECONDS >= 2, blissflixx.POWER_GRACE_SECONDS
        assert blissflixx.POWER_GRACE_SECONDS <= 10, blissflixx.POWER_GRACE_SECONDS

    def test_and_it_is_waited_out_before_systemctl_is_run(self, api, monkeypatch):
        """
        The wait is a pause, not a number subtracted from somewhere else. Asserted
        by having systemctl refuse to be run at all until the wait has happened,
        which is the only ordering that actually matters.
        """
        waited = []

        def fake_sleep(seconds):
            waited.append(seconds)

        def fake_run(cmd, **kwargs):
            assert waited, "systemctl was asked before the machine was given time"
            assert waited[0] == blissflixx.POWER_GRACE_SECONDS, waited
            return ok()

        monkeypatch.setattr(blissflixx.time, "sleep", fake_sleep)
        monkeypatch.setattr(blissflixx.subprocess, "run", fake_run)

        api.default("server", fn="reboot")


class TestWhatEveryTabIsTold:
    """The notice reaches tabs by the same road an error does."""

    def test_it_is_in_the_status_poll(self, api, monkeypatch):
        monkeypatch.setattr(blissflixx.subprocess, "run", lambda cmd, **kw: ok())

        api.default("server", fn="reboot")

        status = status_of(api)
        assert status["Msg"] == "Rebooting...", status
        # Error is what makes the bar the tabs already have appear at all.
        assert status["Error"] is True, status
        # And Restart is what stops them being able to dismiss it: the text is
        # the machine saying what it is about to do, not the reader's to close.
        assert status["Restart"] is True, status

    def test_and_it_is_gone_again_when_the_machine_stays_up(self, api, monkeypatch):
        """
        A press that is refused leaves nothing behind. Otherwise every tab would
        sit on "Rebooting..." for as long as the browser is open, on a machine
        that is plainly still there -- which is the same lie as the button
        reporting success for a machine that did nothing.
        """
        monkeypatch.setattr(
            blissflixx.subprocess,
            "run",
            lambda cmd, **kw: ok(returncode=1, stderr="Failed to power off: Access denied"),
        )

        result = api.default("server", fn="reboot")
        assert "Access denied" in result["error"], result

        status = status_of(api)
        assert status.get("Msg", "") == "", status
        assert status.get("Restart") is not True, status

    def test_and_gone_again_when_the_call_itself_fails(self, api, monkeypatch):
        def explode(cmd, **kwargs):
            raise OSError("no systemctl here")

        monkeypatch.setattr(blissflixx.subprocess, "run", explode)

        assert "error" in api.default("server", fn="reboot")
        assert status_of(api).get("Msg", "") == "", status_of(api)

    def test_it_stays_set_when_the_machine_is_actually_going(self, api, monkeypatch):
        """
        Nothing clears it on the way down. The machine is leaving and this
        process is leaving with it, so there is nothing to tidy and nowhere to
        tidy it: the last status a tab ever receives is the one saying so, which
        is what a shutdown wants -- the notice is still up, correctly, forever.
        """
        monkeypatch.setattr(blissflixx.subprocess, "run", lambda cmd, **kw: ok())

        api.default("server", fn="shutdown")

        assert blissflixx.POWER_MESSAGE == "Shutting down..."

    def test_it_does_not_leak_into_other_answers(self, api, monkeypatch):
        """
        Only the poll tabs read carries it -- and only that one function of that
        one module, which is the pair of conditions the hook is written with.

        Anything else and the text turns up in the middle of a channel list, or
        in the answer to the question "what backends are there", where nobody
        would think to look for it and some other code would try to read it as
        something it is not.
        """
        monkeypatch.setattr(blissflixx.subprocess, "run", lambda cmd, **kw: ok())
        blissflixx.POWER_MESSAGE = "Rebooting..."

        for modname, fn in [
            ("channels", "list_enabled"),
            ("playlists", "list_all"),
            ("server", "version"),
            ("playr", "backends"),
            ("playr", "backend"),
        ]:
            answer = api.default(modname, fn=fn)
            assert "Rebooting..." not in repr(answer), (modname, fn, answer)

    def test_the_restart_notice_still_works_alongside_it(self, api, monkeypatch):
        """
        The path this one is modelled on, and it must not have been broken by the
        addition. A restart while a reboot is in flight says what it says.
        """
        monkeypatch.setattr(blissflixx, "RESTARTING", True)
        try:
            status = status_of(api)
            assert status["Msg"] == "Server Restarting & Updating...", status
            assert status["Restart"] is True, status
        finally:
            blissflixx.RESTARTING = False

    def test_nothing_is_said_when_nothing_is_happening(self, api):
        status = status_of(api)

        assert status.get("Msg", "") == "", status
        assert status.get("Restart") is not True, status


class TestAPressTwice:
    """
    The failure that looks like a second failure.

    A double press gets systemd's answer about already rebooting. Treating that
    as the refusal it resembles takes the notice away from every tab a second
    before the machine does what the first press asked -- so the first press
    works and nobody is ever told.
    """

    def test_the_second_press_is_refused(self, api, monkeypatch):
        asked = []
        monkeypatch.setattr(
            blissflixx.subprocess, "run", lambda cmd, **kw: asked.append(cmd) or ok()
        )
        monkeypatch.setattr(
            blissflixx.time, "sleep", lambda s: blissflixx.POWER_MESSAGE or asked.append("waited")
        )

        api.default("server", fn="reboot")
        second = api.default("server", fn="reboot")

        assert asked.count(["systemctl", "reboot"]) == 1, asked
        assert "error" in second, second

    def test_and_the_first_press_keeps_its_notice(self, api, monkeypatch):
        monkeypatch.setattr(blissflixx.subprocess, "run", lambda cmd, **kw: ok())

        api.default("server", fn="reboot")
        api.default("server", fn="shutdown")

        assert blissflixx.POWER_MESSAGE == "Rebooting...", blissflixx.POWER_MESSAGE
        assert status_of(api)["Msg"] == "Rebooting...", status_of(api)

    def test_the_refusal_says_what_is_happening_not_what_was_pressed(self, api, monkeypatch):
        """
        A shutdown is under way and somebody presses reboot. The answer has to
        describe the shutdown.

        It used to describe the button -- "Already trying to reboot" -- because
        the wording was built from the press rather than from what the machine
        was doing. That names the wrong one of the two and sends whoever reads
        it waiting for a reboot that is not coming.
        """
        monkeypatch.setattr(blissflixx.subprocess, "run", lambda cmd, **kw: ok())

        api.default("server", fn="shutdown")
        second = api.default("server", fn="reboot")

        assert "Shutting down..." in second["error"], second
        assert "reboot" not in second["error"], second
        # And this press did nothing, said plainly.
        assert "did nothing" in second["error"], second


class TestTheWordsAreTheOnesThePressedTabShows:
    """
    Two sources, one sentence.

    The tab that pressed the button says this from the click, before the server
    has been asked for anything. Every other tab says it from the server. So the
    two have to be the same words, or the tab you are looking at says
    "Rebooting..." and the one beside it says nothing at all -- which is the
    exact bug this whole arrangement exists to fix, reappearing in a new place.
    """

    def test_the_server_sends_what_the_pressed_tab_says(self):
        source = (pathlib.Path(locations.HTML_PATH) / "js" / "PowerStore.js").read_text()

        for action, message in blissflixx.POWER_MESSAGES.items():
            assert message in source, (
                "%r says %r and the server says %r. Every other tab reads the "
                "server's." % (action, message, message)
            )

    def test_and_both_actions_are_covered(self):
        assert sorted(blissflixx.POWER_MESSAGES) == sorted(blissflixx.POWER_ACTIONS)

    def test_and_the_pressed_tab_still_says_it_from_the_click(self):
        """
        The server's copy is for the tabs that did not press anything, and it
        cannot be the only copy: the tab that pressed is told within a second
        over a poll that is about to stop answering, while the click is already
        in hand.
        """
        source = (pathlib.Path(locations.HTML_PATH) / "js" / "PowerStore.js").read_text()

        assert "self.msg = action === 'shutdown'" in source, source


class TestATabThatOnlyWatched:
    """
    The one that cannot press the button and has to be told.

    Its own powerStore has nothing in it -- nobody pressed anything here -- so
    the quiet it used to get came from nowhere. What it has instead is the
    server's answer, which is what it checks now.
    """

    def _run(self, power_msg, status):
        """
        Run Utils.showError with one tab's two possible reasons to stay quiet.

        powerMsg is what this tab's own store knows; status is what the server
        last said. Returns the routes it navigated to.
        """
        import json
        import os
        import shutil
        import subprocess
        import tempfile

        if shutil.which("node") is None:
            pytest.skip("node is not installed here")

        script = """
        var shown = [];
        function riot() {}
        riot.route = function (p) { shown.push(p); };
        var powerMsg = %(powerMsg)s;
        var powerStore = powerMsg ? { getMsg: function () { return powerMsg; } } : undefined;
        var playerStore = { getStatus: function () { return %(status)s; } };
        %(utils)s
        Utils.showError('/offline/offline');
        console.log(JSON.stringify(shown));
        """
        utils = (pathlib.Path(locations.HTML_PATH) / "js" / "Utils.js").read_text()

        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
            fh.write(script % {"powerMsg": json.dumps(power_msg), "status": json.dumps(status), "utils": utils})
            path = fh.name
        try:
            proc = subprocess.run(["node", path], capture_output=True, text=True, timeout=60)
            assert proc.returncode == 0, proc.stderr
            return json.loads(proc.stdout.strip())
        finally:
            os.unlink(path)

    def test_it_stays_quiet_while_the_machine_is_leaving(self):
        """
        No powerMsg here: this is the tab that only watched, and the notice came
        from the server like an error does. The old check read only the first of
        these two, so a click during somebody else's reboot still replaced the
        page with "please restart the server" while the server was rebooting
        because it had been asked to.
        """
        assert self._run(None, {"Restart": True}) == [], (
            "showError still hijacks the screen in a tab that only watched the "
            "machine go down"
        )

    def test_and_while_this_tab_is_the_one_that_pressed(self):
        assert self._run("Rebooting...", {}) == [], (
            "showError still navigates during a reboot this tab asked for"
        )

    def test_but_not_when_the_server_says_nothing_is_wrong(self):
        """
        A Restart flag the machine has already finished with must not silence a
        real fault for ever. Every fresh poll clears it, so this is the shape
        every ordinary moment has.
        """
        assert self._run(None, {"Msg": "", "Restart": False}) == ["error/offline/offline"], (
            "showError stopped reporting faults that are real"
        )

    def test_and_not_when_there_is_no_status_at_all_yet(self):
        """
        The store's status is {} until the first poll answers. A tab that is
        asked to do something in that moment must still be able to report a
        failure, rather than throwing on a missing field.
        """
        assert self._run(None, {}) == ["error/offline/offline"]

    def test_the_source_does_not_merely_mention_it(self):
        """
        Grepping the file would pass on a line in a comment, which is how this
        started once already. The checks above run the function; this only says
        the thing they rely on is there to be run.
        """
        source = (pathlib.Path(locations.HTML_PATH) / "js" / "Utils.js").read_text()
        code = re.sub(r"//.*", "", source)

        assert "playerStore.getStatus().Restart" in code, (
            "Utils.showError no longer reads the server's own answer"
        )


class TestTheBarRendersIt:
    """Nothing was added to the interface, and this is why that is enough."""

    def test_the_bar_already_shows_the_servers_own_text(self):
        source = (pathlib.Path(locations.HTML_PATH) / "tags" / "playerr.html").read_text()
        assert "opts.msg || opts.status.Msg" in source, source

    def test_and_already_refuses_to_let_it_be_dismissed(self):
        """
        Restart is what hides the close button, which is why the server sets it:
        a notice that a reader could dismiss would come back on the next poll
        and then have to be dismissed again, once a second, forever.
        """
        source = (pathlib.Path(locations.HTML_PATH) / "tags" / "playerr.html").read_text()
        assert "!opts.status.Restart" in source, source
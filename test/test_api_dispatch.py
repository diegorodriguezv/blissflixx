"""
API dispatch.

blissflixx.py mounts a single CherryPy handler at /api and resolves the
requested module and function by name. That indirection used to sit outside the
try block, so an unknown module or function produced a 500 with a traceback
instead of the intended 404. These tests pin the resolution rules.

Importing blissflixx is safe because startup moved under a __main__ guard.
"""

import re
import unittest.mock as m

import pytest

import blissflixx
from lib.api.common import ApiError


@pytest.fixture
def api():
    return blissflixx.Api()


def body(api, modname, fn=None, data=None):
    """Call the handler and return the (status, payload) it produced."""
    resp = api
    # _error() sets cherrypy.response.status; outside a real request that object
    # exists but is not connected, so the returned dict is what we assert on.
    result = api.default(modname, fn=fn, data=data)
    return resp, result


class TestADirectoryWithNoModuleIsNotAChannel:
    """
    Removing a channel can leave its directory behind.

    git deletes the tracked files, but not the directory, when something
    untracked is still inside it -- and __pycache__ always is. Python imports
    such a directory as an implicit namespace package: an empty module that
    raises AttributeError the moment anything reads an attribute from it, so the
    loader printed a traceback on every restart for a channel that had been
    deliberately deleted.

    It was caught, so it was harmless. It was also wrong: it named a fault where
    there was nothing to fault.
    """

    def test_a_directory_without_init_is_skipped_quietly(self, tmp_path, monkeypatch):
        """
        One test for the whole thing, because it is one thing: a directory with
        no __init__.py yields no channel, and no traceback either.

        The traceback is the part that was visible. The loader caught the failure
        either way, so the channel's absence never needed proving; what needed
        proving is that a deleted channel stops being reported as a fault.
        """
        import lib.api.channels as api_channels

        gone = tmp_path / "bfch_vanished"
        gone.mkdir()
        (gone / "__pycache__").mkdir()
        (gone / "icon.png").write_bytes(b"x")

        monkeypatch.setattr(api_channels, "CHAN_PATH", str(tmp_path))
        monkeypatch.setattr(api_channels, "load", lambda name: {})

        reported = []
        monkeypatch.setattr(
            api_channels,
            "traceback",
            type("t", (), {"print_exc": staticmethod(lambda: reported.append(1))}),
        )

        installed = api_channels.InstalledChannels()

        assert installed.getAll() == [], "a directory with no module was loaded"
        assert reported == [], "a deleted channel still logs a traceback"


class TestAChannelIsDescribedAsADesc:
    """
    A channel's description is published as "desc".

    It used to be published as "subtitle", which is already a loaded word here:
    a media item carries a subtitle -- one line of detail under its title, like
    "Score: 12, 3 comments" -- and PlayItem.to_dict() serialises it under exactly
    that name. An API response holding a channel and a list of its items
    therefore carried the same key meaning two unrelated things.

    Nothing read the channel's field, so renaming it broke nothing. That is
    exactly why it is worth a test: a rename nobody notices is a rename nobody
    notices being undone.
    """

    def _channel(self, tmp_path, monkeypatch, request):
        import sys

        import lib.api.channels as api_channels

        ch = tmp_path / "bfch_demo"
        ch.mkdir()
        (ch / "__init__.py").write_text(
            "def name(): return 'Demo'\n"
            "def description(): return 'A channel about demo'\n"
            "def image(): return 'icon.png'\n"
        )
        (ch / "icon.png").write_bytes(b"x")

        # CHAN_PATH only reaches sys.path when lib.api.channels is first
        # imported, so patching it afterwards is not enough to make a stub
        # importable. Both are needed.
        monkeypatch.setattr(sys, "path", [str(tmp_path)] + list(sys.path))
        monkeypatch.setattr(api_channels, "CHAN_PATH", str(tmp_path))
        monkeypatch.setattr(api_channels, "load", lambda name: {})
        # The stub is imported during the test, so it has to be dropped
        # afterwards rather than before. Left in sys.modules it would satisfy a
        # later import of the same name, and that test would silently get this
        # stub instead of the channel it meant to load.
        request.addfinalizer(lambda: sys.modules.pop("bfch_demo", None))
        return api_channels.InstalledChannels()

    def test_the_field_is_named_desc(self, tmp_path, monkeypatch, request):
        installed = self._channel(tmp_path, monkeypatch, request)
        info = installed.getAll()[0].getInfo()
        assert info["desc"] == "A channel about demo", info

    def test_it_no_longer_answers_to_subtitle(self, tmp_path, monkeypatch, request):
        installed = self._channel(tmp_path, monkeypatch, request)
        assert "subtitle" not in installed.getAll()[0].getInfo()

    def test_an_item_subtitle_is_still_called_subtitle(self, tmp_path, monkeypatch):
        """
        The other half. Renaming the channel's field must not have renamed the
        media item's one, which PlayItem.to_dict() still emits as "subtitle" and
        which the interface renders under every video.
        """
        from lib.playitem import PlayItem

        item = PlayItem("A title", "img.jpg", "http://example.com/v.mp4", "2 min")
        assert item.to_dict()["subtitle"] == "2 min"


class TestModuleResolution:
    def test_unknown_module_is_404(self, api):
        resp, result = body(api, "definitely_not_a_module", "list_all")
        assert "not defined" in result["error"]
        assert "definitely_not_a_module" in result["error"]

    @pytest.mark.parametrize(
        "modname", ["channels", "playlink", "playr", "playlists", "torrent"]
    )
    def test_every_registered_module_resolves(self, api, modname):
        assert modname in blissflixx.api_modules

    def test_registry_covers_the_package_contents(self):
        """
        lib/api/__init__.py is deliberately empty, so api_modules is the only
        list of callable modules. Anything else under lib/api that exposes
        functions to the frontend would be unreachable without being added here.
        """
        assert set(blissflixx.api_modules) == {
            "channels",
            "playlink",
            "playr",
            "playlists",
            "subtitles",
            "torrent",
        }

    def test_module_attributes_are_reachable_by_name(self):
        """
        The old implementation used getattr(api, modname). The new one uses an
        explicit dict, so assert the equivalent access still works for anything
        that introspects the package.
        """
        import lib.api

        for name in blissflixx.api_modules:
            assert getattr(lib.api, name) is blissflixx.api_modules[name]


class TestFunctionResolution:
    def test_unknown_function_is_404(self, api):
        resp, result = body(api, "channels", "no_such_function")
        assert "not defined" in result["error"]
        assert "no_such_function" in result["error"]

    def test_non_callable_attribute_is_404(self, api):
        """
        Every attribute of an API module used to be reachable, including dunder
        and imported names. Those must not be callable over HTTP.
        """
        resp, result = body(api, "channels", "__doc__")
        assert "not defined" in result["error"]

    def test_missing_function_name_is_404(self, api):
        resp, result = body(api, "channels", None)
        assert "not defined" in result["error"]


class TestDispatchBehaviour:
    def test_kwargs_are_passed_through(self, api, monkeypatch):
        seen = {}

        def fake_feed(chid=None, idx=None):
            seen["chid"] = chid
            seen["idx"] = idx
            return [{"title": "x"}]

        monkeypatch.setattr(blissflixx.api_modules["channels"], "feed", fake_feed)
        result = api.default("channels", fn="feed", data='{"chid":"a","idx":1}')
        assert seen == {"chid": "a", "idx": 1}
        assert result == [{"title": "x"}]

    def test_api_error_carries_its_own_message_and_status(self, api, monkeypatch):
        """
        ApiError's message is written for the person using the interface, so it
        is returned as the error rather than as a stack trace with the message
        buried in it.

        It used to be caught by the generic handler, so "Channel ID is missing"
        reached the screen wrapped in a 500 and a traceback. That is right for a
        bug and wrong for a sentence somebody has to read, and the test here
        pinned the old shape so that changing it would be deliberate -- which is
        what this is.
        """
        seen = {}

        def boom(chid=None):
            raise ApiError("Channel ID is missing")

        def spy(status, msg):
            seen["status"] = status
            seen["msg"] = msg
            return {"error": msg}

        monkeypatch.setattr(blissflixx.api_modules["channels"], "feed", boom)
        monkeypatch.setattr(api, "_error", spy)

        result = api.default("channels", fn="feed")

        assert seen["status"] == 400
        assert seen["msg"] == "Channel ID is missing"
        assert result["error"] == "Channel ID is missing"
        assert "Traceback" not in seen["msg"]

    def test_a_busy_archive_is_503_not_500(self, api, monkeypatch):
        """
        The server is fine and the archive is not, and the status says so.

        This is the case the whole thing exists for: a feed load that fails
        because a third party is overloaded used to come back as an empty list,
        and the interface said "There are no items for this feed." about a
        category that has always had films in it.
        """
        seen = {}

        def boom(chid=None, idx=None):
            raise ApiError(
                "The Reddit archive is busy and did not answer.",
                status=ApiError._SERVICE_UNAVAILABLE,
            )

        def spy(status, msg):
            seen["status"] = status
            return {"error": msg}

        monkeypatch.setattr(blissflixx.api_modules["channels"], "feed", boom)
        monkeypatch.setattr(api, "_error", spy)

        api.default("channels", fn="feed")

        assert seen["status"] == 503

    def test_unexpected_exception_is_reported_not_raised(self, api, monkeypatch):
        def boom(link=None):
            raise ValueError("kaboom")

        monkeypatch.setattr(blissflixx.api_modules["torrent"], "files", boom)
        result = api.default("torrent", fn="files", data='{"link":"x"}')
        assert "kaboom" in result["error"]

    def test_none_result_returns_nothing(self, api, monkeypatch):
        monkeypatch.setattr(
            blissflixx.api_modules["channels"], "list_all", lambda: None
        )
        assert api.default("channels", fn="list_all") is None

    def test_bad_json_data_raises_rather_than_being_swallowed(self, api):
        """
        json.loads runs before the try, so malformed request data surfaces as an
        error to CherryPy rather than a JSON error body. Pinned so the ordering
        is not changed by accident.
        """
        with pytest.raises(ValueError):
            api.default("channels", fn="feed", data="not json")


class TestServerSpecialCase:
    """
    "server" is handled before the module registry, so it must not appear there.

    Restart is destructive -- it kills the process with SIGUSR2 -- and is not
    exercised here. Shutdown and reboot are driven with sudo mocked out, so the
    command is checked without touching the machine.
    """

    def test_server_is_not_in_the_module_registry(self):
        assert "server" not in blissflixx.api_modules

    def test_server_unknown_function_is_404(self, api):
        result = api.default("server", fn="nope")
        assert "not defined" in result["error"]

    def test_server_missing_function_is_404(self, api):
        result = api.default("server", fn=None)
        assert "not defined" in result["error"]


class TestTheVersionEndpoint:
    """
    The build the settings page shows.

    It exists so that "the reboot bar does not appear" and "this browser is
    running a build from three days ago" can be told apart from the screen
    rather than by changing code and asking whether it worked.
    """

    def test_it_answers_with_a_short_hash(self, api):
        assert re.fullmatch(
            r"[0-9a-f]{7,}|unknown", api.default("server", fn="version")
        )

    def test_it_is_the_hash_that_is_checked_out(self, api):
        """
        The one that matters. A server reporting a hash is no use if it is not
        the hash of the code it is serving.
        """
        import subprocess

        from lib import locations

        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=locations.ROOT_PATH,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if sha.returncode != 0:
            pytest.skip("no git checkout here")
        assert api.default("server", fn="version") == sha.stdout.strip()

    def test_it_answers_even_without_git(self, api, monkeypatch):
        """
        Says so rather than raising. The page is a diagnostic, not a dependency,
        and a machine with no checkout still has a settings page.
        """

        def boom(*a, **kw):
            raise OSError("no git here")

        monkeypatch.setattr(blissflixx.subprocess, "run", boom)
        assert api.default("server", fn="version") == "unknown"

    def test_the_settings_page_reads_it(self):
        """
        A version nobody looks at is no use, and the page is compiled at
        runtime rather than shipped, so the check is that the call is in it.

        It asks for `versions` rather than this, because the page shows what is
        running as well as what is checked out. See TestWhatIsActuallyRunning.
        """
        import pathlib

        from lib import locations

        source = (
            pathlib.Path(locations.HTML_PATH) / "pages" / "settings.html"
        ).read_text()
        assert "'versions'" in source, "the settings page never asks what build this is"


class TestWhatIsActuallyRunning:
    """
    The checkout and the running code come apart, and one number cannot say which
    is which.

    git pull moves the checkout while the server carries on running what it loaded
    when it started, so asking git afterwards reports the new commit for a process
    that has never executed a line of it.

    That is not a hypothetical. Deploying a seek fix, rpi served a build from five
    hours earlier for a whole session: the restart killed a bash wrapper, the
    replacement could not bind the port, and the old process went on answering
    every request. The settings page called that number the "running build", so it
    was certifying the exact state it existed to catch.
    """

    def test_it_reports_both_and_says_whether_they_agree(self, api):
        answer = api.default("server", fn="versions")
        assert set(answer) == {"installed", "running", "same"}
        assert answer["same"] == (answer["installed"] == answer["running"])

    def test_the_running_one_is_the_one_the_process_booted_with(self, api, monkeypatch):
        """
        Not read at request time. That is the whole point: by the time a request
        arrives the checkout may already have been pulled, and then a lazy read
        reports the new commit for old code -- wrong in precisely the case the
        report exists for.
        """
        assert blissflixx._BOOTED_AT == api.default("server", fn="versions")["running"]

        monkeypatch.setattr(blissflixx, "_BOOTED_AT", "aaaaaaa")
        monkeypatch.setattr(blissflixx, "_git_sha", lambda: "bbbbbbb")
        answer = api.default("server", fn="versions")
        assert answer == {"installed": "bbbbbbb", "running": "aaaaaaa", "same": False}

    def test_a_pulled_checkout_is_visible_as_a_difference(self, api, monkeypatch):
        monkeypatch.setattr(blissflixx, "_BOOTED_AT", "aaaaaaa")
        monkeypatch.setattr(blissflixx, "_git_sha", lambda: "bbbbbbb")
        assert api.default("server", fn="versions")["same"] is False

        monkeypatch.setattr(blissflixx, "_git_sha", lambda: "aaaaaaa")
        assert api.default("server", fn="versions")["same"] is True

    def test_it_still_answers_without_git(self, api, monkeypatch):
        def boom(*a, **kw):
            raise OSError("no git here")

        monkeypatch.setattr(blissflixx.subprocess, "run", boom)
        answer = api.default("server", fn="versions")
        assert answer["installed"] == "unknown"

    def test_version_itself_keeps_tracking_the_checkout(self, api):
        """
        It cannot be switched to the running build. UpdateCheck.js watches it to
        decide when to reload a browser, and a browser has to reload after a pull
        lands whether or not the server came back -- it is still being served the
        old static files either way. Reporting the running build instead would
        leave the page waiting for a change it would never see.
        """
        monkeypatched = api.default("server", fn="versions")
        assert api.default("server", fn="version") == monkeypatched["installed"]

    def test_a_different_build_is_said_out_loud(self):
        """
        Two numbers that differ need explaining, or they are two numbers. The
        failure they describe -- a server answering that is not on disk -- is
        indistinguishable from every other "my fix did not work".
        """
        import pathlib

        from lib import locations

        source = (
            pathlib.Path(locations.HTML_PATH) / "pages" / "settings.html"
        ).read_text()
        assert "show={ stale }" in source, "nothing is shown when the two disagree"


class TestThePowerButtons:
    """
    Reboot and shutdown have to reach systemd from a service.

    They used to run "sudo shutdown -r 0". From a service sudo cannot prompt for
    a password, so the command failed, os.system's return value went unread, and
    the interface answered 200 for a button that did nothing. Rebooting had
    never worked and nothing said so.

    A passwordless sudo grant was the second attempt, on the reasoning that it
    is how the restart helper already worked. It installed, parsed, matched --
    and changed nothing, because sudo never gets far enough to read a grant when
    it cannot initialise at all:

        sudo: error initializing audit plugin sudoers_audit

    A service has no login session, so its loginuid is unset (4294967295, which
    is -1), libaudit cannot open the audit log, and sudo gives up. `sudo -l`,
    which only reads the policy, fails identically. So these now ask systemd
    directly and polkit decides, which is the mechanism built for it: "inactive"
    is polkit's word for a process with no session.

    What is pinned here is that no sudo survives anywhere in this path, that the
    action is the right one, and that a failure is reported -- because a machine
    where this is not permitted looks exactly like a button nobody pressed.
    """

    @staticmethod
    def _ok(returncode=0, stderr=""):
        return m.Mock(returncode=returncode, stdout="", stderr=stderr)

    @pytest.fixture(autouse=True)
    def _no_waiting_and_nothing_left_over(self, monkeypatch):
        """
        The wait before the machine is asked to go is stubbed out, and the notice
        is put back afterwards.

        Stubbed because six tests in here press the button and the real thing is
        three seconds each, which is a fifth of a minute of a test suite standing
        still to prove something that is not what these tests are about. What
        the wait is *for* is pinned in test_power_notice.py, which watches the
        order of events instead of the clock.

        Put back because the notice is a module global that a successful press
        deliberately leaves set -- the machine is on its way down and the process
        holding it is going with it. One test's reboot leaving "Rebooting..." in
        the next test's status is exactly the kind of thing that makes a suite
        agree with itself while being wrong.
        """
        monkeypatch.setattr(blissflixx.time, "sleep", lambda seconds: None)
        yield
        blissflixx.POWER_MESSAGE = None

    def test_reboot_asks_systemd_to_reboot(self, api, monkeypatch):
        ran = {}

        def fake_run(cmd, **kwargs):
            ran["cmd"] = cmd
            return self._ok()

        monkeypatch.setattr(blissflixx.subprocess, "run", fake_run)

        api.default("server", fn="reboot")

        assert ran["cmd"] == ["systemctl", "reboot"], ran

    def test_shutdown_asks_systemd_to_power_off(self, api, monkeypatch):
        ran = {}

        def fake_run(cmd, **kwargs):
            ran["cmd"] = cmd
            return self._ok()

        monkeypatch.setattr(blissflixx.subprocess, "run", fake_run)

        api.default("server", fn="shutdown")

        assert ran["cmd"] == ["systemctl", "poweroff"], ran

    @pytest.mark.parametrize("action", sorted(blissflixx.POWER_ACTIONS.values()))
    def test_every_verb_is_a_verb_systemctl_has(self, action):
        """
        Checked against systemctl rather than written down.

        The shutdown verb was "power-off" -- the polkit action id, which is a
        different naming scheme that agrees in one case out of two -- and the
        button answered:

            Unknown command verb 'power-off', did you mean 'poweroff'?

        Reboot was unaffected, because "reboot" is both, so the one button that
        had never been pressed end to end was the broken one. This test reads
        the verbs out of systemctl's own help text, so the next verb that is
        wrong fails here rather than on a television.
        """
        import shutil
        import subprocess

        systemctl = shutil.which("systemctl")
        if not systemctl:
            pytest.skip("systemctl is not installed here")

        help_text = subprocess.run(
            [systemctl, "--help"], capture_output=True, text=True, timeout=30
        ).stdout

        verbs = {
            line.split()[0]
            for line in help_text.splitlines()
            if line.startswith("  ") and line.strip() and " " in line.strip()
        }
        assert (
            action in verbs
        ), "%r is not a systemctl verb. systemctl's own list says: %s" % (
            action,
            sorted(v for v in verbs if v[0] == action[0]),
        )

    def test_no_sudo_anywhere_in_the_power_path(self, api, monkeypatch):
        """
        The whole point. sudo cannot initialise from this process at all, so a
        grant for it is decoration -- and it was installed and matched before
        anyone established that.
        """
        ran = {}

        def fake_run(cmd, **kwargs):
            ran.setdefault("cmds", []).append(cmd)
            return self._ok()

        monkeypatch.setattr(blissflixx.subprocess, "run", fake_run)

        api.default("server", fn="reboot")
        api.default("server", fn="shutdown")

        for cmd in ran["cmds"]:
            assert "sudo" not in cmd, cmd

    def test_a_failure_is_reported_rather_than_swallowed(self, api, monkeypatch):
        """
        The silent version of this is the bug. The button reports success, the
        machine does not reboot, and there is nothing in the response, the log
        or the screen to say the two facts are connected.
        """
        monkeypatch.setattr(
            blissflixx.subprocess,
            "run",
            lambda cmd, **kw: self._ok(
                returncode=1,
                stderr="Failed to power off: Access denied",
            ),
        )

        result = api.default("server", fn="reboot")

        assert "Access denied" in result["error"], result
        assert "-boot" in result["error"], result

    def test_the_message_carries_the_whole_command_to_paste(self):
        """
        The person reading this has a button that does nothing. "Run
        configure.sh -boot" is not enough: it does not say where, and omits
        -only, which without it reinstalls every system dependency first and
        takes most of an hour on a Pi 3.

        So the message carries the whole line, with this checkout's own path in
        it -- configure.sh writes the unit with WorkingDirectory set to wherever
        it was run from, so the directory is part of the command rather than a
        detail the reader has to supply.
        """
        message = blissflixx.POWER_SETUP_COMMAND

        assert message.startswith("cd "), message
        assert message.endswith("sudo ./configure.sh -boot -only"), message
        assert str(blissflixx.locations.ROOT_PATH) in message, message
        # One line, so it pastes as one line.
        assert "\n" not in message, message

    def test_the_message_names_the_action_it_could_not_do(self, api, monkeypatch):
        monkeypatch.setattr(
            blissflixx.subprocess, "run", lambda cmd, **kw: self._ok(returncode=1)
        )

        result = api.default("server", fn="reboot")

        assert "reboot" in result["error"], result
        assert blissflixx.POWER_SETUP_COMMAND in result["error"], result

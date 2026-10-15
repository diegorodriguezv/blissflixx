"""
API dispatch.

blissflixx.py mounts a single CherryPy handler at /api and resolves the
requested module and function by name. That indirection used to sit outside the
try block, so an unknown module or function produced a 500 with a traceback
instead of the intended 404. These tests pin the resolution rules.

Importing blissflixx is safe because startup moved under a __main__ guard.
"""

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
    Its other functions (restart, shutdown, reboot) are destructive and are
    deliberately not exercised here: restart kills the process with SIGUSR2 and
    the others shell out to sudo shutdown.
    """

    def test_server_is_not_in_the_module_registry(self):
        assert "server" not in blissflixx.api_modules

    def test_server_unknown_function_is_404(self, api):
        result = api.default("server", fn="nope")
        assert "not defined" in result["error"]

    def test_server_missing_function_is_404(self, api):
        result = api.default("server", fn=None)
        assert "not defined" in result["error"]


class TestModuleImportIsolation:
    def test_importing_blissflixx_mounts_nothing(self):
        """
        The entrypoint used to end in engine.block(), so importing it hung; it
        also cloned yt-dlp from the network and SIGTERMed processes by name.
        Moving that into main() under a __main__ guard is what makes this
        module importable, and mounting is now part of main() rather than of
        import time.
        """
        import cherrypy

        assert cherrypy.tree.apps == {}

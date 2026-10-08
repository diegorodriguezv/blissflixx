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

    def test_api_error_becomes_a_500_with_traceback(self, api, monkeypatch):
        """
        ApiError carries a user-facing message ("Channel ID is missing") but is
        caught by the generic handler, so it still surfaces as a 500. This is
        pre-existing behaviour, pinned here so that changing it is deliberate.
        """

        def boom(chid=None):
            raise ApiError("Channel ID is missing")

        monkeypatch.setattr(blissflixx.api_modules["channels"], "feed", boom)
        result = api.default("channels", fn="feed")
        assert "error" in result
        assert "Channel ID is missing" in result["error"]

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

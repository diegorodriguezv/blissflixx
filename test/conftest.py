"""
Shared test fixtures.

Two things need neutralising before any BlissFlixx module is imported for
testing:

1. Settings. lib/settings writes to data/settings/ derived from lib/locations,
   which resolves against the checkout. Tests must never read or write the real
   user's playlists and channel settings, so SETTINGS_PATH is redirected into a
   temporary directory. The module caches loaded values in _cache, so that cache
   is cleared per test too.

2. Network. lib/chanutils has no timeouts by default at import, but the real risk
   is a test quietly hitting a live site and returning data that changes
   tomorrow. The autouse fixture below fails any test that tries to open a
   socket. Tests that genuinely need the network must be marked
   `@pytest.mark.network`.

Note lib/api/channels.py appends CHAN_PATH and PLUGIN_PATH to sys.path at
import time. That is the mechanism installed channels are loaded by, so it is
left in place; pyproject.toml puts both the repo root and chls/ on the path so
channels can also be imported directly by name.
"""

import socket

import pytest

from lib import settings as settings_module


class NetworkAccessDenied(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def temp_settings(tmp_path, monkeypatch):
    """
    Point lib.settings at a throwaway directory and clear its cache.

    Exposed as a fixture as well as being autouse, because a test that exercises
    subtitle language handling needs to seed settings and then invalidate the
    cache the way a fresh process would.
    """
    monkeypatch.setattr(settings_module, "SETTINGS_PATH", str(tmp_path / "settings"))
    (tmp_path / "settings").mkdir(parents=True, exist_ok=True)
    settings_module._cache.clear()
    yield settings_module
    settings_module._cache.clear()


@pytest.fixture
def settings(temp_settings):
    """
    lib.settings with an empty cache.

    save() writes through to the cache, so call settings._cache.clear() after
    saving to mimic what a restarted process would see.
    """
    return settings_module


@pytest.fixture(autouse=True)
def no_network(request):
    """
    Fail loudly if an unmarked test tries to reach the network.

    Only INET sockets are blocked. AF_UNIX is not network access: tests use unix
    sockets to exercise the VLC and mpv control transports against a fake server
    in tmp_path, which is exactly the kind of thing this fixture should permit
    rather than force to be mocked away. A MagicMock cannot check that a command
    is line-terminated; a real socket peer can, and that difference already let a
    bug through.
    """
    if request.node.get_closest_marker("network"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection
    inet = (socket.AF_INET, socket.AF_INET6)

    def deny(*args, **kwargs):
        raise NetworkAccessDenied(
            "This test attempted a network connection. Mark it with "
            "@pytest.mark.network if that is intended."
        )

    def guard_connect(sock, *args, **kwargs):
        if sock.family in inet:
            return deny()
        return real_connect(sock, *args, **kwargs)

    def guard_connect_ex(sock, *args, **kwargs):
        if sock.family in inet:
            return deny()
        return real_connect_ex(sock, *args, **kwargs)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(socket.socket, "connect", guard_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guard_connect_ex)
    monkeypatch.setattr(socket, "create_connection", deny)
    try:
        yield
    finally:
        monkeypatch.undo()
        assert real_connect is not None
        assert real_connect_ex is not None
        assert real_create_connection is not None

"""
Backend registry.

Backends are selected by name from the "player" setting:

    data/settings/player   {"backend": "mpv"}

Leaving it unset keeps the historical behaviour, which was to choose between
the two omxplayer variants using the http and dlsrv flags. That path is still
here in _legacy_backend() and is deliberately unchanged, so upgrading does not
silently switch anyone's player.
"""

from .mpvproc import MpvProcess
from .omxproc import OmxplayerProcess
from .omxproc2 import OmxplayerProcess2

#: name -> backend class. Add an entry here to make a backend selectable.
BACKENDS = {
    "omxplayer": OmxplayerProcess,
    "omxplayer-keys": OmxplayerProcess2,
    "mpv": MpvProcess,
}

#: Used when no backend is configured.
DEFAULT_BACKEND = None


def backend_names():
    return sorted(BACKENDS)


def get_backend(name):
    """
    Return the backend class registered under name.

    Raises KeyError for an unknown name rather than falling back silently, so a
    typo in the settings file surfaces instead of playing with the wrong player.
    """
    return BACKENDS[name]


def describe(name):
    """Name, display name and capabilities, for reporting to the UI."""
    cls = get_backend(name)
    instance = cls()
    return {
        "backend": name,
        "name": instance.name(),
        "capabilities": sorted(instance.capabilities),
    }

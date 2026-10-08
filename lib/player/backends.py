"""
Backend registry.

Backends are selected by name. Which one is active comes from the "player"
setting:

    data/settings/player   {"backend": "mpv"}

How a given backend is invoked comes from its own settings file:

    data/settings/player-mpv   {"audio_device": "alsa/hdmi:CARD=vc4hdmi,DEV=0"}

Registry keys are names, not classes, so the same class can be registered twice
with different configuration. That is how a Pi 4 and a Pi 5, or a desktop and a
Pi, sit side by side in one checkout without a platform conditional:

    BACKENDS = {"mpv": MpvProcess, "mpv-pi5": MpvProcess}

Leaving "backend" unset keeps the historical behaviour: the http and dlsrv flags
choose between the two omxplayer variants. That path lives in
_Player._legacy_backend() and is deliberately unchanged, so upgrading does not
silently switch anyone's player.
"""

from ..settings import load
from .mpvproc import MpvProcess
from .omxproc import OmxplayerProcess
from .omxproc2 import OmxplayerProcess2

#: name -> backend class. Add an entry to make a backend selectable. Registering
#: the same class under two names gives two independent configurations.
BACKENDS = {
    "omxplayer": OmxplayerProcess,
    "omxplayer-keys": OmxplayerProcess2,
    "mpv": MpvProcess,
}

#: Used when no backend is configured. None means "keep the legacy choice".
DEFAULT_BACKEND = None


def backend_names():
    return sorted(BACKENDS)


def backend_class(name):
    """
    The class registered under name.

    Raises KeyError for an unknown name rather than falling back silently, so a
    typo in the settings file surfaces instead of playing with the wrong player.
    """
    return BACKENDS[name]


def settings_name(name):
    """The settings file a backend's configuration lives in."""
    return "player-" + name


def resolve_config(name):
    """
    Defaults for the named backend with data/settings/player-<name> merged over.

    An absent file yields the defaults unchanged, which is what keeps a
    checkout with no settings at all behaving exactly as it did.
    """
    cls = backend_class(name)
    return dict(cls.defaults, **load(settings_name(name)))


def get_backend(name):
    """A configured instance of the named backend."""
    return backend_class(name)(resolve_config(name))


def describe(name):
    """Name, display name and capabilities, for reporting to the UI."""
    instance = get_backend(name)
    return {
        "backend": name,
        "name": instance.name(),
        "capabilities": sorted(instance.capabilities),
        "binary": instance.opt("binary"),
    }

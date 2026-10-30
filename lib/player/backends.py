"""
Backend registry.

There is one player backend: VLC. It decodes H.264 in hardware, sends audio to
HDMI and burns in subtitles without an expensive compositing path.

How it is invoked comes from its settings file, merged over the defaults the
class declares:

    data/settings/player-vlc   {"audio_device": "hdmi:CARD=vc4hdmi,DEV=0"}

The registry is keyed by name rather than holding a class directly, and a class
may be registered under more than one name with independent configuration. That
is what lets one checkout describe several machines -- a Pi 4 and a Pi 5, say --
without a platform conditional in the code.
"""

from ..settings import load
from .vlcproc import VlcProcess

#: name -> backend class. Registering the same class under two names gives two
#: independent configurations.
BACKENDS = {
    "vlc": VlcProcess,
}

#: The player a play uses.
DEFAULT_BACKEND = "vlc"


def backend_names():
    """Every registered backend."""
    return sorted(BACKENDS)


def backend_class(name):
    """The class registered under name. Raises KeyError for an unknown name."""
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

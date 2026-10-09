"""
Backend registry.

Backends are selected by name. Which one is active comes from the "player"
setting:

    data/settings/player   {"backend": "vlc"}

Leaving it unset selects DEFAULT_BACKEND. The pseudo-name "legacy" selects the
behaviour from before backends existed, where the http and dlsrv flags chose
between the two omxplayer variants; that path lives in
_Player._legacy_backend() and is unchanged.

How a given backend is invoked comes from its own settings file:

    data/settings/player-mpv   {"audio_device": "alsa/hdmi:CARD=vc4hdmi,DEV=0"}

Registry keys are names, not classes, so the same class can be registered twice
with different configuration. That is how a Pi 4 and a Pi 5, or a desktop and a
Pi, sit side by side in one checkout without a platform conditional:

    BACKENDS = {"mpv": MpvProcess, "mpv-pi5": MpvProcess}
"""

from ..settings import load
from .gstproc import GStreamerProcess
from .mpvproc import MpvProcess
from .omxproc import OmxplayerProcess
from .omxproc2 import OmxplayerProcess2
from .vlcproc import VlcProcess

#: name -> backend class. Add an entry to make a backend selectable. Registering
#: the same class under two names gives two independent configurations.
BACKENDS = {
    "omxplayer": OmxplayerProcess,
    "omxplayer-keys": OmxplayerProcess2,
    "vlc": VlcProcess,
    "mpv": MpvProcess,
    "gstreamer": GStreamerProcess,
}

#: Used when no backend is configured.
#:
#: omxplayer only runs on obsolete Raspberry Pi OS, so VLC is the default: it
#: decodes H.264 in hardware, sends audio to HDMI and burns in subtitles without
#: the expensive GStreamer compositing path. The two omxplayer backends remain
#: selectable by name for anyone who needs them.
DEFAULT_BACKEND = "vlc"

#: Selects the pre-abstraction behaviour, where the http and dlsrv flags decide
#: between the two omxplayer variants. Not a backend in its own right, but
#: reachable by name so that path is not lost now that a backend is chosen
#: without consulting those flags.
LEGACY_BACKEND = "legacy"


def backend_names():
    """
    Every registered backend.

    This is the registry and nothing else, so every name here resolves through
    get_backend(). The legacy pseudo-backend is not in it, because it is not a
    class; see selectable_names() for what a client may choose from.
    """
    return sorted(BACKENDS)


def selectable_names():
    """Everything a client may select: the registry plus the legacy name."""
    return sorted(BACKENDS) + [LEGACY_BACKEND]


def is_known(name):
    """Whether name can be selected, without raising."""
    return name in BACKENDS or name == LEGACY_BACKEND


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


def active_backend_name():
    """
    Which backend the next play will use.

    Falls back to DEFAULT_BACKEND, so an unconfigured install still has a
    definite answer. The value is not validated here: an unknown name in the
    settings file is reported when playback is attempted, where the traceback is
    useful, rather than being silently swapped for something else.
    """
    return load("player").get("backend", DEFAULT_BACKEND)


def describe(name):
    """
    What a backend is and what it can do, for reporting to the UI.

    Capabilities matter here more than the name. The playbar renders one set of
    controls regardless of which backend is active, so a UI that wants to hide
    buttons a backend would drop needs this rather than a hardcoded list.
    """
    if name == LEGACY_BACKEND:
        # Not a single backend: it picks between two depending on the flags,
        # so its capabilities depend on what it resolves to at playback time.
        # Reported as the richer of the two, which is what it mostly resolves to.
        keys = get_backend("omxplayer-keys")
        info = {
            "backend": name,
            "name": "omxplayer (chosen per item)",
            "capabilities": sorted(keys.capabilities),
            "binary": "omxplayer / omxplayer.bin",
            "note": "the player is chosen from the http and dlsrv flags",
        }
        return info
    instance = get_backend(name)
    info = {
        "backend": name,
        "name": instance.name(),
        "capabilities": sorted(instance.capabilities),
        "binary": instance.opt("binary"),
    }
    # A backend with no control surface needs saying out loud, so a caller does
    # not offer buttons that will be dropped. gstreamer is the case that matters.
    if not instance.capabilities:
        info["note"] = "this backend cannot be paused, seeked or adjusted"
    return info

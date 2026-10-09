"""
Player backends.

A backend is the pipeline stage that actually renders media. omxplayer was the
only one for years, and it came in two flavours that looked like two players but
differed only in how control commands reach the process: one talks dbus, the
other writes keystrokes into a FIFO. lib/player/player.py picked between them
with two booleans, which decided a control transport rather than naming a
player.

PlayerBackend separates those concerns:

- build_command(args) is pure and works from a fresh instance, so the command a
  backend will run is assertable without the binary being installed.
- control(action) is per-backend, because the transport genuinely differs.
- declares() is the capability set, so the UI can know what a backend can do
  rather than sending it actions it silently drops.

OmxplayerBackend holds the readiness parsing that both omxplayer variants shared
verbatim. It lives here rather than on PlayerBackend because it parses
omxplayer-specific output; MpV produces no equivalent stream of status lines and
overrides readiness instead.

Command building is configuration driven. Each backend declares a `defaults`
dict and the settings file for it, data/settings/player-<name>, merges over the
top. That is what makes the same checkout usable on a Pi 4 and a Pi 5, or on a
desktop during development: the hardware strings differ (an ALSA card name, a
KMS plane id) and they are values rather than branches in code.

    data/settings/player-mpv   {"audio_device": "alsa/hdmi:CARD=vc4hdmi,DEV=0"}
    data/settings/player-vlc   {"audio_device": "hdmi:CARD=vc4hdmi,DEV=0"}

A config file may be absent or partial; anything it does not set keeps its
default. Unrecognised keys are logged and ignored rather than raising, because a
backend must stay usable when a settings file written for a different version
names something that no longer exists.
"""

from abc import abstractmethod

import cherrypy

from .processpipe import ExternalProcess, ProcessException

# Capabilities a backend can advertise. The player stage uses this to report
# what the current backend supports, so the UI does not offer controls that
# would be dropped.
CAP_PAUSE = "pause"
CAP_STOP = "stop"
CAP_SEEK = "seek"
CAP_VOLUME = "volume"
CAP_SUBTITLES = "subtitles"
CAP_AUDIO_TRACK = "audio_track"

ALL_CAPABILITIES = frozenset(
    {CAP_PAUSE, CAP_STOP, CAP_SEEK, CAP_VOLUME, CAP_SUBTITLES, CAP_AUDIO_TRACK}
)


class PlayerBackend(ExternalProcess):
    """Base for a stage that renders media and accepts control actions."""

    #: Overridden per backend. Subset of ALL_CAPABILITIES.
    capabilities = frozenset()

    #: Configuration this backend falls back to. Every key a backend reads must
    #: appear here, so that resolve_config can tell a typo from a real setting.
    defaults = {}

    def __init__(self, config=None):
        super().__init__()
        self.config = self._merge(config)

    def _merge(self, config):
        """Overlay a settings dict on the defaults, logging unknown keys."""
        if not config:
            return dict(self.defaults)
        known = set(self.defaults)
        unknown = sorted(set(config) - known)
        if unknown:
            cherrypy.log(
                "Unknown "
                + self.__class__.__name__
                + " setting(s) ignored: "
                + ", ".join(unknown)
            )
        merged = dict(self.defaults)
        merged.update({k: v for k, v in config.items() if k in known})
        return merged

    def opt(self, key):
        """Read a configured value. The key must be in defaults."""
        return self.config[key]

    def declares(self, capability):
        return capability in self.capabilities

    def supports(self, *capabilities):
        return all(self.declares(c) for c in capabilities)

    @abstractmethod
    def build_command(self, args):
        """
        Return the command to run, as an argv list or a shell string.

        Pure: it must depend only on args, never on state left behind by
        start(), so it is testable without the backend installed.
        """

    def _get_cmd(self, args):
        return self.build_command(args)

    @abstractmethod
    def control(self, action):
        """
        Act on a playback control action.

        Backends do not all control the player the same way, and a backend should
        not assume omxplayer's model. There are three shapes in practice:

        - Keystroke, no acknowledgement. omxplayer.bin reads keys from a FIFO.
          Writing the key is all there is; nothing reports whether it was acted
          on, so a caller cannot distinguish "delivered" from "ignored".
        - Request/response. VLC's rc interface and mpv's JSON IPC both reply, so
          delivery can be observed and logged.
        - None. gst-launch exposes no IPC at all, so every action is dropped.

        control() returns False when the command could not be delivered at all
        (no socket, nothing running), and otherwise does not raise. It is called
        for actions the caller believes are supported; check declares() if an
        unsupported action should be reported rather than ignored.
        """

    def _ready(self):
        """
        Block until the backend has started successfully, raising
        ProcessException if it cannot. The default suits backends with no
        status output to wait for.
        """


class OmxplayerBackend(PlayerBackend):
    """
    Shared behaviour for the two omxplayer variants.

    Both print progress on stdout and signal success with a Metadata: or
    Duration: line. These two _ready implementations were byte-for-byte
    identical before this class existed.
    """

    def _ready(self):
        while True:
            line = self._readline(self.start_timeout)
            if line.startswith("have a nice day"):
                raise ProcessException("omxplayer failed to start")
            elif line.startswith("Vcodec id unknown:"):
                raise ProcessException("Unsupported video codec")
            elif "Metadata:" in line:
                break
            elif "Duration:" in line:
                break

    #: Seconds to wait for the first line of output. Subclasses override.
    start_timeout = None

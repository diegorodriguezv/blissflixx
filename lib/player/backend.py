"""
Player backends.

A backend is the pipeline stage that actually renders media.

PlayerBackend separates three concerns:

- build_command(args) is pure and works from a fresh instance, so the command a
  backend will run is assertable without the binary being installed.
- control(action) is per-backend, because the transport genuinely differs.
- declares() is the capability set, so the UI can know what a backend can do
  rather than sending it actions it silently drops.

Command building is configuration driven. Each backend declares a `defaults`
dict and the settings file for it, data/settings/player-<name>, merges over the
top. That is what makes the same checkout usable on a Pi 4 and a Pi 5, or on a
desktop during development: the hardware strings differ (an ALSA card name, a
KMS plane id) and they are values rather than branches in code.

    data/settings/player-vlc   {"audio_device": "hdmi:CARD=vc4hdmi,DEV=0"}
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
#: Nudging subtitles earlier or later, for when the audio and the subtitles
#: drift apart. Distinct from CAP_SUBTITLES, which is about which track is
#: showing at all.
#:
#: VLC's cli interface has no verb for it -- the whole command table has no
#: sub-delay -- but it has a "key" command that simulates a hotkey press, and
#: the subtitle-delay hotkey moves exactly the variable VLC renders with. See
#: VlcProcess._shift_subtitle_delay.
CAP_SUBTITLE_DELAY = "subtitle_delay"

ALL_CAPABILITIES = frozenset(
    {
        CAP_PAUSE,
        CAP_STOP,
        CAP_SEEK,
        CAP_VOLUME,
        CAP_SUBTITLES,
        CAP_AUDIO_TRACK,
        CAP_SUBTITLE_DELAY,
    }
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

        A backend must not assume one player's model. There are three shapes in
        practice: keystroke with no acknowledgement (the player reads keys from a
        FIFO and nothing says whether it acted), request/response (VLC's cli
        interface replies, so delivery can be observed and logged), and none at
        all (a pipeline with no control surface drops every action).

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

    #: Seconds to wait for the first line of output. Subclasses override.
    start_timeout = None

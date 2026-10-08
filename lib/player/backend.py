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
"""

from abc import abstractmethod

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

        Called for actions the caller believes are supported; check declares()
        if an unknown action should be reported rather than ignored.
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

"""
GStreamer backend.

The most efficient of the three by CPU: a direct hardware pipeline with no
player framework in the way. It also cannot do two things, and this module is
honest about both rather than pretending otherwise.

No control. gst-launch is a one-shot pipeline runner. There is no IPC surface at
all -- no pause, no seek, no volume -- so capabilities is empty and control() is
a documented no-op. Playback is start and watch; stopping works only because
ProcessPipe SIGKILLs the process group. Real pause and seek would need
gst-python's GstPlayer, which drives an existing pipeline rather than launching
one, and that is the way back if it is ever needed.

No subtitles. Text overlay needs the GStreamer compositing path, which is the
expensive part this backend exists to avoid.

The pipeline is a configurable template because its parts are machine specific.
kmssink's plane-id and connector-id are ids out of /sys/class/drm that differ
between models, and the decoder element differs by what is installed: the Pi
carries v4l2h264dec, while a development machine without the Pi's kernel packages
does not have it at all.
"""

import os
import time

from .backend import PlayerBackend
from .processpipe import ProcessException

GST_BIN = "gst-launch-1.0"
_START_TIMEOUT = 15

#: The pipeline, with {outfile} substituted at the source. Kept flat here rather
#: than written as a multi-line gst-launch description, because this backend
#: passes the elements as separate argv words and needs no shell quoting for the
#: "!" separators.
_PIPELINE = (
    "{source}",
    "matroskademux",
    "name=demux",
    "demux.video_0",
    "!",
    "queue",
    "!",
    "h264parse",
    "!",
    "{video_decoder}",
    "!",
    "kmssink",
    "plane-id={plane_id}",
    "connector-id={connector_id}",
    "demux.audio_0",
    "!",
    "queue",
    "!",
    "{audio_decoder}",
    "!",
    "audioconvert",
    "!",
    "audioresample",
    "!",
    "alsasink",
    "device={audio_device}",
)


class GStreamerProcess(PlayerBackend):
    """
    Renders via gst-launch-1.0.

    Built as argv words with shell=False. gst-launch joins its arguments into a
    pipeline description, and separate words mean the "!" separators need no
    escaping, which a shell string would require.
    """

    #: Nothing. See the module docstring: gst-launch has no control surface.
    capabilities = frozenset()

    defaults = {
        "binary": GST_BIN,
        "pipeline": _PIPELINE,
        # A local file is still being written by the download stage, and
        # filesrc stops at the current end of it. The dlsrv stage serves it over
        # http instead, which this source can follow.
        "http_source": "souphttpsrc",
        # kmssink ids, from /sys/class/drm on the target machine. They differ
        # between models, which is why they are configurable.
        "plane_id": 98,
        "connector_id": 35,
        "audio_device": "hdmi:CARD=vc4hdmi,DEV=0",
        # Present on the Pi. Absent on a development machine without the Pi's
        # kernel packages, which is why the decoder is configurable rather than
        # hardcoded.
        "video_decoder": "v4l2h264dec",
        "audio_decoder": "avdec_eac3",
        "start_timeout": _START_TIMEOUT,
    }

    def __init__(self, config=None):
        super().__init__(config=config)
        self.shell = False

    @property
    def start_timeout(self):
        return self.opt("start_timeout")

    def build_command(self, args):
        outfile = args["outfile"]
        if outfile.startswith("http"):
            source = "{} location={}".format(self.opt("http_source"), outfile)
        else:
            # gst-launch rebuilds its pipeline by joining argv with spaces, and
            # its parser accepts no quoting of a property value: neither
            # backslash-escaped, double-quoted nor single-quoted spaces work.
            # A path with a space in it therefore cannot be expressed at all, and
            # emitting the command anyway produces
            # "WARNING: erroneous pipeline: syntax error", which says nothing
            # about the cause. Fail with the reason instead.
            #
            # The paths this backend normally sees have no spaces: the dlsrv
            # url, and /tmp/blissflixx/bf.out for torrent downloads. Only a
            # user-chosen file:// path with a space reaches this.
            if " " in outfile:
                raise ProcessException(
                    "gst-launch cannot open a path containing a space: " + outfile
                )
            source = "filesrc location=" + outfile
        cmd = [self.opt("binary"), "-q"]
        cmd += [
            element.format(
                source=source,
                plane_id=self.opt("plane_id"),
                connector_id=self.opt("connector_id"),
                audio_device=self.opt("audio_device"),
                video_decoder=self.opt("video_decoder"),
                audio_decoder=self.opt("audio_decoder"),
            )
            for element in self.opt("pipeline")
        ]
        return cmd

    def name(self):
        return "gstreamer"

    def _ready(self):
        """
        gst-launch prints nothing on success, so there is nothing to wait for.

        Instead this only guards against the pipeline failing to launch at all:
        an unknown element makes gst-launch exit immediately, and waiting for
        output that will never come would turn a typo into a hang.
        """
        time.sleep(0.2)
        if self.proc is not None and self.proc.poll() is not None:
            raise ProcessException(
                self._drain_error() or "gst-launch exited immediately"
            )

    def _drain_error(self):
        while True:
            line = self._readline(1)
            if not line:
                return None
            return line

    def control(self, action):
        """
        Deliberately does nothing.

        gst-launch offers no way to act on a running pipeline. Stopping works
        through ProcessPipe.stop(), which SIGKILLs the process group, so the
        UI's stop button is still honoured.
        """

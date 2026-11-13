"""
A black picture on the display while nothing is playing.

When a film stops, VLC gives the display back, and the TV loses its signal
until the next film starts. A black clip held on the same output keeps the
signal. It is taken down before the next film starts, because a second VLC
cannot take the display while the first is holding it.

Off in tests (see conftest), and a no-op wherever ffmpeg or vlc is missing.
"""

import os
import signal
import subprocess
import threading

from ..locations import DATA_PATH

ENABLED = True

CLIP = os.path.join(DATA_PATH, "hold", "black.mp4")
#: A short clip, looped. Long enough to be a loop rather than a flicker, short
#: enough to make in about a second on the Pi.
_CLIP_SECONDS = 2
_STOP_GRACE = 3

#: The same output the films use, so the signal is the one the TV is on. Read
#: the film's own options from the player settings if these ever differ.
_VLC = [
    "vlc",
    "-I", "dummy",
    "--intf=cli",
    "--loop",
    "--no-video-title-show",
    "--vout=drm_vout",
    "--drm-vout-module=vc4",
    "--aout=adummy",
]

_lock = threading.Lock()
_proc = None


def _make_clip():
    if os.path.isfile(CLIP):
        return True
    os.makedirs(os.path.dirname(CLIP), exist_ok=True)
    partial = CLIP + ".part"
    try:
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:size=1920x1080:rate=25",
                "-t", str(_CLIP_SECONDS),
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-f", "mp4", partial,
            ],
            stdin=subprocess.DEVNULL,
            timeout=60,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    os.replace(partial, CLIP)
    return True


def show():
    """Put the black clip on the display, unless it is already there."""
    global _proc
    if not ENABLED:
        return
    with _lock:
        if _proc is not None and _proc.poll() is None:
            return
        if not _make_clip():
            return
        try:
            _proc = subprocess.Popen(
                _VLC + [CLIP],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError:
            _proc = None


def hide():
    """Take the black clip down, so the next film can have the display."""
    global _proc
    with _lock:
        proc, _proc = _proc, None
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        proc.wait(timeout=_STOP_GRACE)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
    if proc.stdin is not None:
        proc.stdin.close()

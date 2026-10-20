"""
Well-known locations, resolved relative to this file.

The values are str rather than Path on purpose. Most of them are interpolated
into shell commands by lib/player (OMX_CMD, the peerflix argv, getsubs.py,
yt-dlp) or passed to subprocess, so keeping them as plain strings avoids
scattering str() calls across the riskiest code in the project. Use pathlib
internally, as below, but keep the exported types.
"""

from pathlib import Path

# This file is repo/lib/locations/__init__.py, so three parents up from the file
# is the checkout root: parent -> locations, parent -> lib, parent -> repo.
#
# absolute() rather than resolve(): the old code used os.path.abspath, which
# does not follow symlinks, and a checkout reached through a symlink should
# keep resolving to the same place it did before.
_ROOT = Path(__file__).absolute().parents[2]

ROOT_PATH = str(_ROOT)
LIB_PATH = str(_ROOT / "lib")
HTML_PATH = str(_ROOT / "html")
DATA_PATH = str(_ROOT / "data")
BIN_PATH = str(_ROOT / "bin")
PLIST_PATH = str(_ROOT / "data" / "playlists")
SETTINGS_PATH = str(_ROOT / "data" / "settings")
#: The checkout's own JavaScript runtime, unpacked here by configure.sh.
#:
#: Not installed system-wide on purpose. yt-dlp requires node 22 and the
#: distribution ships 20, but replacing the system node would move the runtime
#: that npm and peerflix were installed with -- a change to the machine, not to
#: this project. See lib/player/nodejs.py, which is the one place that knows.
NODE_PATH = str(_ROOT / "data" / "node")
#: A Netscape cookies.txt, if the user has put one there.
#:
#: Not a setting. It is a credential rather than configuration, and it belongs
#: somewhere git already ignores -- data/ is. yt-dlp passes it to the sites that
#: will not talk to a Raspberry Pi without one; absent is the normal case and
#: changes nothing. See lib/player/ytdlproc.py.
COOKIES_PATH = str(_ROOT / "data" / "cookies.txt")
CHAN_PATH = str(_ROOT / "chls")
PLUGIN_PATH = str(_ROOT / "plugins")

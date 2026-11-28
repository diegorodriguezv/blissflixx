"""
Filesystem locations, resolved rather than assumed.

The checkout root is found by looking for what the application ships with --
``pyproject.toml`` and ``html/`` -- starting from this package and walking up,
not by counting parents from this file. Where a module sits is a fact about one
particular layout, and the layout changed once already when ``lib`` became
``blissflixx``. ``BLISSFLIXX_ROOT`` names the root explicitly, for an
installation that keeps its files somewhere else.

Data is not part of the code even when it lives under the checkout: it is
written at runtime and belongs to the installation. ``BLISSFLIXX_DATA`` moves
it (a backup volume, a larger card); the default is ``<root>/data``, which is
where existing installations already keep it.

The values are str rather than Path on purpose. Most of them are interpolated
into shell commands by the player (OMX_CMD, the torrent argv, yt-dlp) or
passed to subprocess, so keeping them as plain strings avoids
scattering str() calls across the riskiest code in the project. Use pathlib
internally, as below, but keep the exported types.
"""

import os
from pathlib import Path

#: Environment variable naming the checkout explicitly. Needed when the code is
#: installed somewhere that does not sit inside the checkout -- a wheel in a
#: virtualenv, with html/ and data/ elsewhere.
ENV_ROOT = "BLISSFLIXX_ROOT"

#: Environment variable naming the directory runtime state lives in.
ENV_DATA = "BLISSFLIXX_DATA"

#: What a directory must contain to be the checkout root. ``pyproject.toml``
#: alone can belong to a workspace above it; ``html/`` alone could be any web
#: directory, so both are required.
_ROOT_MARKERS = ("pyproject.toml", "html")


def find_root(start=None):
    """
    The checkout root, or an explanation of why there is not one.

    ``start`` is this package's directory when not given. absolute() rather than
    resolve(): a checkout reached through a symlink should keep resolving to the
    same place it did before.
    """
    explicit = os.environ.get(ENV_ROOT) or ""
    if explicit:
        return Path(explicit).expanduser().absolute()

    start = (
        Path(start).expanduser().absolute()
        if start
        else Path(__file__).absolute().parent
    )
    for candidate in (start, *start.parents):
        if all((candidate / marker).exists() for marker in _ROOT_MARKERS):
            return candidate

    raise RuntimeError(
        "BlissFlixx cannot find its checkout: no directory above %s contains "
        "%s. Set %s to the directory that does."
        % (start, " and ".join(_ROOT_MARKERS), ENV_ROOT)
    )


def data_dir(root):
    """Where runtime state lives: ``BLISSFLIXX_DATA``, else ``<root>/data``."""
    explicit = os.environ.get(ENV_DATA) or ""
    if explicit:
        return Path(explicit).expanduser().absolute()
    return Path(root) / "data"


_ROOT = find_root()
_DATA = data_dir(_ROOT)
_PACKAGE = Path(__file__).absolute().parent

ROOT_PATH = str(_ROOT)
#: The application package itself. Not "lib": that was never what it was.
PACKAGE_PATH = str(_PACKAGE)
HTML_PATH = str(_ROOT / "html")
DATA_PATH = str(_DATA)
BIN_PATH = str(_ROOT / "bin")
PLIST_PATH = str(_DATA / "playlists")
SETTINGS_PATH = str(_DATA / "settings")
#: The checkout's own JavaScript runtime, unpacked here by configure.sh.
#:
#: Not installed system-wide on purpose. yt-dlp requires node 22 and the
#: distribution ships 20, but replacing the system node would move the runtime
#: the distribution's own packages were installed with -- a change to the
#: machine, not to this project. See blissflixx/player/nodejs.py, which is the
#: one place that knows.
NODE_PATH = str(_DATA / "node")
#: A Netscape cookies.txt, if the user has put one there.
#:
#: Not a setting. It is a credential rather than configuration, and it belongs
#: somewhere git already ignores -- data/ is. yt-dlp passes it to the sites that
#: will not talk to a Raspberry Pi without one; absent is the normal case and
#: changes nothing. See blissflixx/player/ytdlproc.py.
COOKIES_PATH = str(_DATA / "cookies.txt")
CHAN_PATH = str(_PACKAGE / "channels")
PLUGIN_PATH = str(_ROOT / "plugins")
#: Committed third-party executables that nothing here builds. See
#: vendor/README.md for what each one is and where it came from.
VENDOR_PATH = str(_ROOT / "vendor")

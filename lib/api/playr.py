import urllib.parse

from ..chanutils.torrent import torrent_idx
from ..player import Player
from ..player.backends import active_backend_name, describe, is_known, selectable_names
from ..settings import save
from .common import ApiError
from .torrent import is_torrent_url


def _save_subs_prefs(subs):
    if "lang" in subs:
        save("subtitles", {"lang": subs["lang"]})


def play(url=None, title=None, subs=None):
    if url is None:
        raise ApiError("Play url is undefined")
    if subs is not None:
        _save_subs_prefs(subs)
    obj = urllib.parse.urlparse(url)
    if obj.scheme == "file":
        Player.playLocalFile(obj.path, title)
    elif is_torrent_url(url):
        Player.playTorrent(url, torrent_idx(url), title, subs)
    else:
        Player.playYtdl(url, title, subs)


def control(action=None):
    if action is None:
        raise ApiError("Action is undefined")
    Player.control(action)


def status():
    return Player.status()


def backend(name=None):
    """
    Describe one backend, or the one that is active.

    The playbar renders one fixed set of controls whatever backend is running,
    so a client needs to know what the active backend can actually do before it
    offers a button that would be silently dropped. gstreamer is the case that
    matters: it cannot be paused, seeked or adjusted.
    """
    if name is None:
        name = active_backend_name()
    if not is_known(name):
        raise ApiError("Unknown player backend '" + str(name) + "'")
    return describe(name)


def backends():
    """
    Every selectable backend, with the active one marked.

    The order matches selectable_names(), so 'legacy' is listed alongside the
    real backends rather than hidden, since it is a genuine choice.
    """
    active = active_backend_name()
    listed = []
    for name in selectable_names():
        info = describe(name)
        info["active"] = name == active
        listed.append(info)
    return listed


def set_backend(name=None):
    """
    Change the active backend. Takes effect from the next play.

    The item already playing keeps the backend it started with: swapping the
    player out from under a running pipeline would mean rebuilding it, and the
    pipe does not retain the url and subtitles it was built from.
    """
    if name is None:
        raise ApiError("Backend name is undefined")
    if not is_known(name):
        raise ApiError(
            "Unknown player backend '"
            + str(name)
            + "'. Available: "
            + ", ".join(selectable_names())
        )
    save("player", {"backend": name})
    return describe(name)

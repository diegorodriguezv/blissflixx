import urllib.parse

from ..chanutils.torrent import torrent_idx
from ..player import Player
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

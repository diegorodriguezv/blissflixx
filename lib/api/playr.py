import urllib.parse

import cherrypy

from ..chanutils.torrent import torrent_idx
from ..player import Player
from ..player.backends import active_backend_name, describe, is_known, selectable_names
from ..settings import save
from . import playlists
from .common import ApiError
from .torrent import is_torrent_url


def _save_subs_prefs(subs):
    if "lang" in subs:
        save("subtitles", {"lang": subs["lang"]})


def _item_builder(item):
    """
    A callable that builds the pipeline for one playlist item.

    Routed the way a play is routed. What a playlist stores is the url an item
    was added from, which says nothing about how to play it -- that decision
    lives here and is asked for again per item.
    """
    url = item.get("url", "")
    title = item.get("title", "")
    subs = item.get("subs")
    obj = urllib.parse.urlparse(url)
    if obj.scheme == "file":
        return lambda: Player.buildLocalFile(obj.path, title)
    if is_torrent_url(url):
        return lambda: Player.buildTorrent(url, torrent_idx(url), title, subs)
    return lambda: Player.buildYtdl(url, title, subs)


def _place_in_playlist(plid, itemnum, url):
    """
    The playlist a play belongs to: builders for all of it, and this item's
    index. (None, None) when the play is not part of a playlist.

    The url is checked against the one at that index rather than trusted. A
    playlist can be edited, or a remote one refreshed, between the page being
    drawn and the row being pressed; if they disagree then this play is not
    that item any more, and playing it on its own is more honest than dragging
    a playlist behind it.
    """
    if plid is None or itemnum is None:
        return None, None
    try:
        items = playlists.get(plid)["items"]
        index = int(itemnum)
    except Exception as exc:
        # A missing or unreadable playlist is no reason to refuse to play the
        # thing that was asked for. Said out loud because it is not normal.
        cherrypy.log("could not read playlist %r for a play: %s" % (plid, exc))
        return None, None
    if index < 0 or index >= len(items) or items[index].get("url") != url:
        return None, None
    return [_item_builder(item) for item in items], index


def play(url=None, title=None, subs=None, plid=None, itemnum=None):
    if url is None:
        raise ApiError("Play url is undefined")
    if subs is not None:
        _save_subs_prefs(subs)
    playlist, index = _place_in_playlist(plid, itemnum, url)
    obj = urllib.parse.urlparse(url)
    if obj.scheme == "file":
        Player.playLocalFile(obj.path, title, playlist, index)
    elif is_torrent_url(url):
        Player.playTorrent(url, torrent_idx(url), title, subs, playlist, index)
    else:
        Player.playYtdl(url, title, subs, playlist, index)


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

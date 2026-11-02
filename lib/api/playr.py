import urllib.parse

import cherrypy

from ..chanutils.torrent import torrent_idx
from ..player import Player
from ..settings import save
from . import playlists
from .common import ApiError
from .torrent import is_torrent_url


#: Mirrors one play may carry. A channel can have thirty-five in the catalogue;
#: the player tries them in order and keeps going until one works.
_MAX_SOURCES = 50


def _save_subs_prefs(subs):
    if "lang" in subs:
        save("subtitles", {"lang": subs["lang"]})


def _sources(url, sources):
    """
    The URLs this play may try, the one asked for first.

    Only http(s): the player's fallback builds another stream stage, and a
    magnet or a file path there would be a different kind of play pretending to
    be the same one. None means the row carried no list at all, which is how a
    YouTube film or a torrent stays untracked and without fallbacks.
    """
    if not isinstance(sources, (list, tuple)):
        return None
    wanted = []
    for candidate in [url] + list(sources):
        if not isinstance(candidate, str) or not candidate:
            continue
        if urllib.parse.urlparse(candidate).scheme not in ("http", "https"):
            continue
        if candidate not in wanted:
            wanted.append(candidate)
        if len(wanted) >= _MAX_SOURCES:
            break
    return wanted or None


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
    sources = item.get("sources")
    obj = urllib.parse.urlparse(url)
    if obj.scheme == "file":
        return lambda: Player.buildLocalFile(obj.path, title)
    if is_torrent_url(url):
        return lambda: Player.buildTorrent(url, torrent_idx(url), title, subs)
    return lambda: Player.buildYtdl(url, title, subs, sources)


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


def play(url=None, title=None, subs=None, plid=None, itemnum=None, sources=None):
    if url is None:
        raise ApiError("Play url is undefined")
    if subs is not None:
        _save_subs_prefs(subs)
    playlist, index = _place_in_playlist(plid, itemnum, url)
    mirrors = _sources(url, sources)
    obj = urllib.parse.urlparse(url)
    if obj.scheme == "file":
        Player.playLocalFile(obj.path, title, playlist, index)
    elif is_torrent_url(url):
        Player.playTorrent(url, torrent_idx(url), title, subs, playlist, index)
    else:
        Player.playYtdl(url, title, subs, playlist, index, mirrors)


def add_subtitles(chosen=None, lang=None):
    """
    Subtitles for the film already playing, from the control panel.

    Nothing is restarted: a torrent would have to re-verify its download first,
    which on a film twenty minutes in is not a trade anyone wants.
    """
    if not chosen:
        raise ApiError("No subtitles were chosen")
    try:
        Player.add_subtitles(chosen, lang)
    except RuntimeError as exc:
        raise ApiError(str(exc))
    return True


def control(action=None):
    if action is None:
        raise ApiError("Action is undefined")
    Player.control(action)


def status():
    return Player.status()


def debug():
    """Everything the panel shows: playback, source, player, machine."""
    return Player.debug()

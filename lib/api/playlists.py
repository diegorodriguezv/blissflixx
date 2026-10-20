import calendar
import glob
import json
import os
import re
import time
import urllib.parse

import requests

from ..locations import PLIST_PATH
from ..playitem import PlayItemList, PlaylistItem
from .common import ApiError

REMOTE_REFRESH_INT = 3600

#: How long to wait for a remote playlist.
#:
#: The refresh happens on read -- inside get(), which list() calls for every
#: playlist -- so an unbounded wait is not one slow page, it is a playlists page
#: that never arrives while a server somewhere sits on the connection.
REMOTE_TIMEOUT = 15

#: The icon a channel without a tvg-logo gets. PlaylistItem reads img out of the
#: dict and raises without one, so every item built here carries something.
_DEFAULT_IMG = "/img/icons/list.svg"

#: The schemes this player can play. The rest of what an M3U can contain -- udp,
#: rtp, srt, rtsp -- is multicast or a protocol the yt-dlp pipeline does not
#: speak, and an item that cannot play is not listed: the same rule the Reddit
#: feed applies to a channel link, and for the same reason.
_PLAYABLE = ("http", "https")

_EXTINF = re.compile(r"^#EXTINF:")
_ATTR = re.compile(r'\s*([\w-]+)="([^"]*)"')
_DURATION = re.compile(r"\s*-?[\d.]+\s*")


def _fix_remote_url(url):
    if "dropbox.com" in url:
        idx = url.rfind("?")
        if idx > 0:
            url = url[0:idx]
        url = url + "?raw=1"
    return url


def _get_plid(name):
    plid = _create_plid(name)
    path = _get_path(plid)
    if os.path.isfile(path):
        idx = 1
        while True:
            plid = _create_plid(name, idx)
            path = _get_path(plid)
            if not os.path.isfile(path):
                break
            idx = idx + 1
    return plid


def _extinf(tail):
    """
    The title and the attributes of an #EXTINF line, minus its prefix.

    The attributes are key="value" pairs and the title follows them, after a
    comma. They are consumed from the front rather than split on the last comma,
    so a title that contains one stays whole -- and a quoted logo URL that
    contains one cannot end the title early.
    """
    m = _DURATION.match(tail)
    if m:
        tail = tail[m.end() :]
    attrs = {}
    idx = 0
    while True:
        m = _ATTR.match(tail, idx)
        if not m:
            break
        attrs[m.group(1).lower()] = m.group(2)
        idx = m.end()
    title = tail[idx:]
    if title.startswith(","):
        title = title[1:]
    return title.strip(), attrs


def _name_from_url(url):
    """A bare URL's last path segment, for a list with no #EXTINF lines."""
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    return name or url


def _parse_m3u(text, base_url=None):
    """
    The items in an M3U/M3U8 channel list.

    The extended form is a comment carrying the name and then the URL:

        #EXTINF:-1 tvg-logo="http://.../logo.png" group-title="News",Channel
        http://stream/url.m3u8

    tvg-logo becomes the thumbnail and group-title the subtitle, so an imported
    list looks like the channels do. A list of bare URLs is read too, with the
    file name as the title, because that is what a hand-made one looks like.
    """
    # An HLS manifest is the other thing that arrives with an .m3u8 name: the
    # renditions of one stream rather than a list of channels. Read as a
    # playlist it produces one broken item, so it is refused with the place
    # that can use it.
    if any(l.strip().startswith("#EXT-X-") for l in text.splitlines()):
        raise ApiError(
            "That is an HLS stream, not a playlist. Paste it in Play URL instead."
        )

    if text.lstrip()[:200].startswith("<"):
        raise ApiError(
            "That URL returned a web page, not an M3U playlist. "
            "Use the link to the playlist file itself."
        )

    items = []
    pending = None
    group = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            pending = _extinf(line[8:])
            continue
        if line.startswith("#EXTGRP:"):
            group = line[8:].strip()
            continue
        if line.startswith("#"):
            # #EXTVLCOPT, #KODIPROP and the rest set options for the stream
            # below. iptv-org's news list carries 61 of them.
            continue
        title, attrs = pending or ("", {})
        pending = None
        url = line
        if base_url and "://" not in url:
            # A relative path means the list is a directory of streams.
            url = urllib.parse.urljoin(base_url, url)
        if urllib.parse.urlparse(url).scheme not in _PLAYABLE:
            continue
        item = {
            "title": title or _name_from_url(url),
            "img": attrs.get("tvg-logo") or _DEFAULT_IMG,
            "url": url,
        }
        subtitle = attrs.get("group-title") or group
        if subtitle:
            item["subtitle"] = subtitle
        items.append(item)

    if not items:
        raise ApiError("No channels found in that playlist. Is it an M3U list?")
    return items


def _playlist_title(text, url):
    """
    The name the playlist gets.

    An M3U has no title field, so #PLAYLIST: if the list carries one and the
    file's name otherwise -- "news.m3u" becomes "news", which is the name the
    person who downloaded it already knows it by.
    """
    for line in text.splitlines():
        if line.strip().upper().startswith("#PLAYLIST:"):
            return line.split(":", 1)[1].strip()
    name = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path))
    if name.lower().endswith((".m3u", ".m3u8")):
        name = name.rsplit(".", 1)[0]
    return name or "Remote playlist"


def _fetch_remote(url):
    try:
        r = requests.get(url, timeout=REMOTE_TIMEOUT)
    except requests.RequestException as exc:
        raise ApiError(
            "Could not fetch that playlist: " + str(exc),
            ApiError._SERVICE_UNAVAILABLE,
        )
    if r.status_code >= 300:
        raise ApiError("Fetch remote playlist returned: " + str(r.status_code))
    # Decoded here rather than with r.text: a text/* reply with no charset is
    # read as latin-1 by requests, which mangles the names of most lists outside
    # western Europe. utf-8-sig also takes the BOM off the first line.
    try:
        text = r.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = r.content.decode("latin-1")
    playlist = {"title": _playlist_title(text, url), "items": _parse_m3u(text, url)}
    playlist["url"] = url
    if not playlist["title"].endswith("[REMOTE]"):
        playlist["title"] = playlist["title"] + " [REMOTE]"
    return playlist


def new_remote(url=None):
    if url is None:
        raise ApiError("Remote playlist URL must be defined")
    url = _fix_remote_url(url)
    playlist = _fetch_remote(url)
    plid = _get_plid(playlist["title"])
    playlist["plid"] = plid
    save(playlist)
    return get(plid)


def new(name=None):
    if name is None:
        raise ApiError("Playlist name must be defined")
    playlist = _empty_playlist(name)
    plid = _get_plid(name)
    playlist["plid"] = plid
    save(playlist)
    return get(plid)


def delete(plid=None):
    path = _get_path(plid)
    try:
        os.remove(path)
    except Exception:
        pass


def list():
    names = _get_playlists()
    del_action = {"label": "Delete Playlist", "type": "delplaylist"}
    edit_action = {"label": "Edit Playlist", "type": "editplaylist"}
    results = []
    for n in names:
        actions = [del_action]
        playlist = get(n)
        if not "img" in playlist:
            playlist["img"] = "/img/icons/list.svg"
        if not "url" in playlist:
            actions.append(edit_action)
        playlist["actions"] = actions
        results.append(playlist)
    return results


def add_item(plid=None, item=None):
    if (plid is None) or (item is None):
        raise ApiError("Playlist ID and item must be defined")
    playlist = get(plid)
    playlist["items"].append(item)
    save(playlist)


def del_item(plid=None, item=None):
    if (plid is None) or (item is None):
        raise ApiError("Playlist ID and item must be defined")
    playlist = get(plid)
    items = playlist["items"]
    for i in items:
        if i["url"] == item["url"]:
            items.remove(i)
            break
    save(playlist)


def _refresh_remote(playlist, plid):
    fpath = _get_path(plid)
    mtime = os.path.getmtime(fpath)
    ntime = calendar.timegm(time.gmtime())
    if ntime - mtime > REMOTE_REFRESH_INT:
        try:
            fresh = _fetch_remote(playlist["url"])
        except ApiError as exc:
            # The copy on disk is the last one that worked, and it is still a
            # playlist. A source that has gone away must not take the page with
            # it: this used to raise, and one unreachable URL emptied the
            # playlists page the cached copy was sitting on.
            print("Could not refresh remote playlist %s: %s" % (plid, exc))
            # Counted as an attempt, so the next read does not try again and
            # wait out the timeout on every page load.
            os.utime(fpath, None)
        else:
            fresh["plid"] = plid
            save(fresh)
            playlist = fresh
    return playlist


def get(plid=None):
    if plid is None:
        raise ApiError("Playlist ID must be defined")
    playlist = json.load(open(_get_path(plid), "r"))
    remote = False
    if not playlist:
        playlist = _empty_playlist()
    elif "url" in playlist:
        remote = True
        playlist = _refresh_remote(playlist, plid)
    playlist["plid"] = plid
    results = PlayItemList()
    itemnum = 0
    for item in playlist["items"]:
        results.add(PlaylistItem(item, plid, itemnum, remote))
        itemnum = itemnum + 1
    playlist["items"] = results.to_dict()
    return playlist


def save(playlist=None):
    if playlist is None:
        raise ApiError("Playlist must be defined")
    plid = playlist.pop("plid", None)
    playlist.pop("actions", None)
    for item in playlist["items"]:
        item.pop("actions", None)
        item.pop("playlist", None)
        item.pop("itemnum", None)
        if "subs" in item:
            item["subs"].pop("lang", None)
    json.dump(playlist, open(_get_path(plid), "w"))


def _empty_playlist(title=""):
    return {"title": title, "items": []}


def _get_path(plid):
    return PLIST_PATH + "/" + plid + ".bfpl"


def _get_playlists():
    paths = glob.glob(os.path.join(PLIST_PATH, "*.bfpl"))
    playlists = []
    for p in paths:
        playlists.append(os.path.basename(p)[:-5])
    return sorted(playlists)


def _create_plid(name, idx=None):
    plid = name.replace(" ", "_")
    if idx is not None:
        plid = plid + "_" + str(idx)
    return plid

import os
import urllib.parse

import requests

from ..chanutils import UrlInfo
from ..chanutils.torrent import is_torrent
from ..playitem import PlayItem, PlayItemList
from .common import ApiError
from .torrent import set_torridx

MAX_TITLE_LEN = 100


def _link_title(link):
    """
    A name for a link that has no page title of its own.

    The Play URL box takes a stream as readily as a page, and a stream has no
    <title> to read: an .m3u8 begins with #EXTM3U. Its last path segment is what
    was pasted, and is what the person will recognise in the list.
    """
    path = urllib.parse.urlparse(link).path.rstrip("/")
    return urllib.parse.unquote(os.path.basename(path)) or link


def item(link=None):
    if not link:
        raise ApiError("Item URL must be defined")
    results = PlayItemList()
    if link.lower().startswith("http"):
        try:
            info = UrlInfo(link)
        except requests.RequestException as exc:
            raise ApiError(
                "Could not fetch that URL: " + str(exc),
                ApiError._SERVICE_UNAVAILABLE,
            )
        title = info.get_html_title() or _link_title(link)
    else:
        title = link
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN] + "..."
    lowercase_link = link.lower()
    subtitle = None
    img = "/img/icons/file-o.svg"
    synopsis = None
    if "youtube.com/" in lowercase_link or "youtu.be/" in lowercase_link:
        subtitle = (
            info.get_youtube_video_length()
            + " - "
            + info.get_youtube_video_channel()
            + " - "
            + info.get_youtube_video_publish_date()
        )
        img = info.get_youtube_video_thumbnail()
        synopsis = info.get_youtube_video_description()
    if is_torrent(link):
        link = set_torridx(link)
    results.add(
        PlayItem(title, img, link, subtitle=subtitle, synopsis=synopsis, subs={})
    )
    return results.to_dict()

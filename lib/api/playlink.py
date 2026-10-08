from lib.api.torrent import set_torridx
from lib.chanutils import UrlInfo
from lib.chanutils.torrent import is_torrent
from lib.playitem import PlayItem, PlayItemList

from .common import ApiError

MAX_TITLE_LEN = 100


def item(link=None):
    if not link:
        raise ApiError("Item URL must be defined")
    results = PlayItemList()
    if link.lower().startswith("http"):
        info = UrlInfo(link)
        title = info.get_html_title()
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

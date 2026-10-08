import re
import subprocess

from ..chanutils import byte_size

# from chanutils.torrent import showmore
from ..playitem import PlayItem, PlayItemList
from .common import ApiError

TRACKERS = (
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.istole.it:6969/announce",
    "udp://www.eddie4.nl:6969/announce",
    "udp://coppersurfer.tk:6969/announce",
    "udp://tracker.btzoo.eu:80/announce",
    "http://explodie.org:6969/announce",
    "udp://9.rarbg.me:2710/announce",
)
HASH_RE = re.compile("[A-F0-9]{40}")


class TorrentPlayItem(PlayItem):
    def __init__(self, title, img, url, subtitle=None, synopsis=None, subs=None):
        url = set_torridx(url)
        PlayItem.__init__(self, title, img, url, subtitle, synopsis, subs)


def torrent2magnet(torrent):
    if torrent.startswith("magnet"):
        return torrent
    matches = HASH_RE.search(torrent.upper())
    if not matches:
        return torrent
    magnet = "magnet:?xt=urn:btih:" + matches.group(0) + "&tr="
    return magnet + "&tr=".join(TRACKERS)


def peerflix_metadata(link):
    # stdin=PIPE so peerflix does not enter interactive mode
    s = subprocess.check_output(["peerflix", link, "-l"], stdin=subprocess.PIPE)
    s = s.decode("utf-8")
    lines = s.split("\n")
    files = []
    for l in lines:
        delim = l.rfind(":")
        if delim == -1:
            break
        if "Verifying downloaded:" in l:
            continue
        files.append((l[20 : delim - 6], l[delim + 7 : -5]))
    return files


def torrent_files(link):
    return peerflix_metadata(torrent2magnet(link))


def showmore(link):
    files = torrent_files(link)
    if not files:
        raise Exception("Unable to retrieve torrent files")
    results = PlayItemList()
    idx = 0
    for f in files:
        subtitle = ""
        if isinstance(f[1], str):
            subtitle = "Size: " + f[1]
        else:
            subtitle = "Size: " + byte_size(f[1])
        url = set_torridx(link, idx)
        img = "/img/icons/file-o.svg"
        idx = idx + 1
        item = PlayItem(f[0], img, url, subtitle)
        results.add(item)
    return results


def files(link=None):
    if not link:
        raise ApiError("Torrent URL must be defined")
    return showmore(link).to_dict()


def set_torridx(url, idx=-1):
    if is_torrent_url(url):
        return re.sub(r"bf_torr_idx\=-?\d+", "bf_torr_idx=" + str(idx), url)
    else:
        if url.find("?") > -1:
            url = url + "&"
        else:
            url = url + "?"
        return url + "bf_torr_idx=" + str(idx)


def is_torrent_url(url):
    return "bf_torr_idx=" in url

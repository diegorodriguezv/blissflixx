import os
import re
import subprocess

from ..chanutils import byte_size

# from chanutils.torrent import showmore
from ..playitem import FileItem, PlayItem, PlayItemList
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

#: What counts as something to watch on a torrent's file list. Everything else
#: -- the .nfo, the .jpg, the subtitles -- is about the torrent rather than in
#: it, and listing all of it made the page a directory listing instead of the
#: episodes of a pack.
VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".webm", ".ts", ".m2ts")

#: Subtitle files a torrent can carry. A .sub/.idx pair is left out on purpose:
#: they are two files that have to be matched up, and VLC needs both.
SUBS_EXTS = (".srt", ".ass", ".ssa", ".vtt")


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


def _has_ext(name, exts):
    return name.lower().endswith(exts)


def _stem(name):
    return os.path.splitext(os.path.basename(name))[0].lower()


def _subs_for(name, subs):
    """
    The subtitles that belong to one file, or all of them if none say so.

    A season pack carries one subtitle per episode, all with the episode's name
    and a language after it -- "Below.S01E01.1080p.eng.srt" for
    "Below.S01E01.1080p.mkv" -- so the name is what pairs them. When nothing
    pairs, every subtitle in the torrent is offered rather than none: the list
    is short and the person can tell which is which from the names.
    """
    stem = _stem(name)
    matched = [s for s in subs if _stem(s[1]).startswith(stem)]
    return matched or subs


def _subs_arg(name, subs):
    if not subs:
        return None
    chosen = _subs_for(name, subs)
    return {"torrent": {"files": [{"idx": i, "name": n} for i, n in chosen]}}


def showmore(link):
    files = torrent_files(link)
    if not files:
        raise Exception("Unable to retrieve torrent files")
    subs = [(i, f[0]) for i, f in enumerate(files) if _has_ext(f[0], SUBS_EXTS)]
    results = PlayItemList()
    for idx, f in enumerate(files):
        if isinstance(f[1], str):
            subtitle = "Size: " + f[1]
        else:
            subtitle = "Size: " + byte_size(f[1])
        img = "/img/icons/file-o.svg"
        if _has_ext(f[0], SUBS_EXTS):
            # Shown so the list says the torrent carries subtitles, and inert:
            # a subtitle is not something to watch.
            results.add(FileItem(f[0], img, subtitle))
        elif _has_ext(f[0], VIDEO_EXTS):
            url = set_torridx(link, idx)
            item = PlayItem(f[0], img, url, subtitle, subs=_subs_arg(f[0], subs))
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

import os
import re
import subprocess
import urllib.parse

from ..chanutils import byte_size

# from chanutils.torrent import showmore
from ..playitem import FileItem, PlayItem, PlayItemList
from .common import ApiError

#: Where to ask for peers, beside whatever the torrent itself names.
#:
#: The old list here was demonii, istole, eddie4, coppersurfer and btzoo, all
#: of which have been gone for years: a .torrent turned into a magnet carried
#: nothing but dead addresses, and a magnet from a channel carried only its
#: own -- often equally dead -- so a busy film could sit at "0 peers" with
#: nobody to ask. These are the ones that answer; see tracker_lines() in the
#: diagnostics for what each of them says about a given torrent.
TRACKERS = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.tiny-vps.com:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "http://tracker.openbittorrent.com:80/announce",
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


def _with_more_trackers(magnet):
    """
    The magnet with any of ours it does not already name.

    A magnet from a channel carries the trackers its uploader chose, which are
    frequently dead, and peerflix will ask only those. Adding ours costs a
    handful of announces and is the difference between "0 peers" on a popular
    film and finding it.
    """
    missing = [
        t for t in TRACKERS if urllib.parse.quote(t, safe="") not in magnet
    ]
    if not missing:
        return magnet
    joiner = "&" if "?" in magnet else "?"
    return magnet + "".join(
        joiner + "tr=" + urllib.parse.quote(t, safe="") for t in missing
    )


def torrent2magnet(torrent):
    if torrent.startswith("magnet"):
        return _with_more_trackers(torrent)
    matches = HASH_RE.search(torrent.upper())
    if not matches:
        return torrent
    magnet = "magnet:?xt=urn:btih:" + matches.group(0)
    return _with_more_trackers(magnet)


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


#: Which episode a name belongs to, if it says: S01E02, s1e2.
_EPISODE_RE = re.compile(r"[sS](\d{1,2})[eE](\d{1,2})")


def _subs_for(name, subs):
    """
    The subtitles that could belong to one file.

    A season pack carries one subtitle per episode, named after the episode and
    then the language -- "Below.S01E01.1080p.eng.srt" for
    "Below.S01E01.1080p.mkv" -- so a name that matches is the episode's own.

    Everything else is offered too unless it names a *different* episode. That
    is what makes a film work: a release like Spider-Man's has four subtitle
    files called things like "English.srt" and "British (SDH).eng.srt", not one
    named after the film, and matching on the name alone left three of them out
    of a list that should have offered all four. A pack is unaffected, because
    its other episodes' subtitles are named after those episodes.
    """
    stem = _stem(name)
    video_episode = _EPISODE_RE.search(name)
    chosen = []
    for idx, sub_name in subs:
        if _stem(sub_name).startswith(stem):
            chosen.append((idx, sub_name))
            continue
        sub_episode = _EPISODE_RE.search(sub_name)
        if sub_episode and (
            video_episode is None or sub_episode.groups() != video_episode.groups()
        ):
            continue
        chosen.append((idx, sub_name))
    # Most probable first: the player takes the first as the one to show.
    chosen.sort(key=lambda s: -_subs_score(s[1], name))
    return chosen


#: Markers in a subtitle's name that mean it was made for people who want the
#: sound described as well. Worth having, rarely what someone means by "the
#: subtitles".
_DESCRIBED_RE = re.compile(r"\b(SDH|HI|CC|HOH)\b", re.IGNORECASE)


def _subs_score(sub_name, video_name):
    """
    How likely this subtitle is the one somebody means.

    The release's own file first -- it was made for this encode, timing and
    all. Then a plain subtitle over a described one, because "English.srt" is
    what most people want and "English (SDH).srt" is what some do. Nothing is
    hidden: all of them are attached, and the player shows this order, so the
    first is the one on screen and the rest are a button away.
    """
    score = 0
    if _stem(sub_name).startswith(_stem(video_name)):
        score += 100
    if not _DESCRIBED_RE.search(_stem(sub_name)):
        score += 10
    return score


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

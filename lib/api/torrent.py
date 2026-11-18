import os
import re
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
    "udp://explodie.org:6969/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://tracker.internetwarriors.net:1337/announce",
    "udp://open.demonii.si:1337/announce",
    "udp://tracker.cyberia.is:6969/announce",
    "udp://tracker.qu.ax:6969/announce",
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


def icon_for_title(title, fallback="/img/icons/film.svg"):
    """Icon representing the kind of release inferred from its name."""
    try:
        from ..chanutils.chanutils import release_kind

        info = release_kind(title or "")
        kind = info.get("kind")
        if kind == "episode":
            return "/img/icons/tv.svg"
        if kind == "tv_pack":
            # Single season vs multi-season/complete series.
            if info.get("pack") or info.get("seasons"):
                return "/img/icons/tv-pack.svg"
            return "/img/icons/tv-season.svg"
        if kind == "movie_pack":
            return "/img/icons/film-pack.svg"
        if kind == "movie":
            return "/img/icons/film.svg"
    except Exception:
        pass
    return fallback


class TorrentPlayItem(PlayItem):
    def __init__(self, title, img, url, subtitle=None, synopsis=None, subs=None):
        url = set_torridx(url)
        PlayItem.__init__(self, title, img, url, subtitle, synopsis, subs)


def _with_more_trackers(magnet):
    """
    The magnet with any of ours it does not already name.

    A magnet from a channel carries the trackers its uploader chose, which are
    frequently dead, and a client asks only for the ones it was told about.
    Adding ours costs a handful of announces and is the difference between
    "0 peers" on a popular film and finding it.
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


def torrent_files(link):
    """
    The files inside a torrent, as (path, size), without playing it.

    The torrent process does this in its listing mode: it fetches the metadata
    the same way a play does and prints the file list. Imported here rather than
    at the top because that module already imports this one.
    """
    from ..player.ltproc import list_files

    return list_files(link)


def _has_ext(name, exts):
    return name.lower().endswith(exts)


def _stem(name):
    return os.path.splitext(os.path.basename(name))[0].lower()


def _file_for_parsing(path):
    """
    The file name, plus a season folder when it has one.

    A torrent's paths start with the pack's own folder ("The Pacific (2010)
    Mp4 1080p/..."), which is not part of the title. A season folder is,
    because it is the only thing that says which season a file belongs to
    when the file itself is just "Episode 09".
    """
    parts = path.replace("\\", "/").split("/")
    if len(parts) >= 2 and re.match(
        r"^(?:season|s)\s*0*\d{1,2}$", parts[-2], re.IGNORECASE
    ):
        return parts[-2] + "/" + parts[-1]
    return parts[-1]


def subtitle_context(pack_title, file_name):
    """
    What to search OpenSubtitles for, from the pack and the file inside it.

    Neither name is enough on its own. A pack title carries the series (and
    the year) but not which episode; a file called "The Pacific Episode 09.mp4"
    carries the episode but no series, and the heuristics read that pack title
    as a film. The context keeps both: the series/title from the pack, the
    season/episode from the file (or its folder).

    The parsing is `release_kind`, the same detector the rows and the subtitle
    search use -- it knows SxxEyy, "Episode 09", parts and season directories.

    A file with an episode number and no season is given season 1. That is
    right for a single-season pack or a miniseries, and it is a value the
    search page shows and lets the person correct; the alternative is a
    search for the whole series.
    """
    from ..chanutils.chanutils import movie_title_year, release_kind

    pack = release_kind(pack_title or "")
    file_info = release_kind(_file_for_parsing(file_name or ""))
    parsed = movie_title_year(pack_title or "")
    pack_kind = pack.get("kind")
    file_kind = file_info.get("kind")

    series = (
        file_info.get("series")
        or (pack.get("series") if pack_kind in ("tv_pack", "episode") else None)
        or parsed.get("title")
    )
    year = parsed.get("year")
    season = file_info.get("season")
    if season is None:
        season = pack.get("season")
    episode = file_info.get("episode")
    if episode is None:
        episode = pack.get("episode")

    if pack_kind in ("tv_pack", "episode") or file_kind == "episode" or episode is not None:
        context = {"series": series}
        if year:
            context["year"] = year
        if season is not None:
            context["season"] = season
        elif episode is not None:
            context["season"] = 1
        if episode is not None:
            context["episode"] = episode
        return context if context.get("series") else None

    context = {}
    title = parsed.get("title") or file_info.get("title") or pack.get("title")
    if title:
        context["title"] = title
    if year:
        context["year"] = year
    return context or None


def _episode_of(name):
    """(season, episode) a name belongs to, using the shared detector."""
    from ..chanutils.chanutils import release_kind

    info = release_kind(name or "")
    if info.get("kind") != "episode":
        return None
    return (info.get("season"), info.get("episode"))


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
    video_episode = _episode_of(name)
    chosen = []
    for idx, sub_name in subs:
        if _stem(sub_name).startswith(stem):
            chosen.append((idx, sub_name))
            continue
        sub_episode = _episode_of(sub_name)
        if sub_episode and (
            video_episode is None or sub_episode != video_episode
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


def showmore(link, title=None):
    files = torrent_files(link)
    if not files:
        raise Exception("Unable to retrieve torrent files")
    raw_subs = [(i, f[0]) for i, f in enumerate(files) if _has_ext(f[0], SUBS_EXTS)]
    # Drop exact duplicates: subtitle files with the same size are almost
    # certainly the same content (common in packs that ship the same track
    # twice under different names). Keep the first occurrence.
    seen_sizes = set()
    subs = []
    for idx, name in raw_subs:
        size = files[idx][1] if isinstance(files[idx][1], int) else 0
        if size and size in seen_sizes:
            continue
        if size:
            seen_sizes.add(size)
        subs.append((idx, name))
    results = PlayItemList()
    for idx, f in enumerate(files):
        if isinstance(f[1], str):
            subtitle = "Size: " + f[1]
        else:
            subtitle = "Size: " + byte_size(f[1])
        if _has_ext(f[0], SUBS_EXTS):
            # Shown so the list says the torrent carries subtitles, and inert:
            # a subtitle is not something to watch. Its own icon -- a caption
            # box rather than a page -- says what it is at a glance.
            results.add(FileItem(f[0], "/img/icons/subtitles.svg", subtitle))
        elif _has_ext(f[0], VIDEO_EXTS):
            url = set_torridx(link, idx)
            # The torrent's own subtitle files first; the OpenSubtitles
            # context beside them, so Add Subtitles on this row knows both the
            # series and which episode it is.
            context = _subs_arg(f[0], subs)
            if title:
                # The OpenSubtitles context needs the pack's name; a channel's
                # showmore has none, and an empty context would be noise. It
                # travels beside a torrent's own subtitle files rather than
                # replacing them, because Add Subtitles still has to say which
                # series and episode the row is.
                known = subtitle_context(title, f[0])
                if known:
                    context = dict(context or {})
                    for key, value in known.items():
                        context.setdefault(key, value)
            item = PlayItem(
                f[0], "/img/icons/film.svg", url, subtitle, subs=context
            )
            results.add(item)
    return results


def files(link=None, title=None):
    if not link:
        raise ApiError("Torrent URL must be defined")
    return showmore(link, title).to_dict()


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

"""
Search all of YouTube, by asking YouTube.

A subreddit is a real answer to "what did people post there" and a poor one to
"find me a video about this": it only knows what somebody happened to submit.
Search asks YouTube itself, through yt-dlp -- the same tool the player already
uses, installed and maintained for exactly these changes -- so there is no API
key, no quota, and no second opinion about what YouTube's own results are.

Flat results only: no formats, no download, no player. Measured on a Pi 3, ten
results in about five seconds.

A search engine was the other idea, and it is not one: from this box
DuckDuckGo answers 202 with no result links and Google answers a consent page.
Both are bot walls around a page of links that would then have to be resolved
as videos anyway.
"""

import json
import subprocess
import sys

from ...playitem import PlayItem, PlayItemList

#: Results per search. Twenty fills a television screen; each further page of
#: YouTube's results costs about a second.
LIMIT = 20

#: Long enough for a slow connection, short enough that the search page does
#: not look hung.
TIMEOUT = 60

#: YouTube's still thumbnail for a video id. Unlike the signed URLs in the
#: results, this one does not expire, so an item stays valid in a playlist.
_THUMB = "https://i.ytimg.com/vi/%s/mqdefault.jpg"

#: What a result is played as.
_WATCH = "https://www.youtube.com/watch?v=%s"


def search(q, limit=LIMIT):
    """PlayItems for the first `limit` results of a YouTube search."""
    items = PlayItemList()
    for video in videos(q, limit):
        title = (video.get("title") or "").strip()
        video_id = (video.get("id") or "").strip()
        if not title or not video_id:
            continue
        items.add(
            PlayItem(
                title,
                _THUMB % video_id,
                _WATCH % video_id,
                _subtitle(video),
            )
        )
    return items


def videos(q, limit=LIMIT):
    """
    yt-dlp's search results, as dicts.

    -j prints one JSON object per line and --flat-playlist keeps it to the
    listing: no formats are extracted and nothing is fetched. --ignore-config
    so a yt-dlp config on the machine cannot change what a search means.
    """
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-j",
        "--flat-playlist",
        "--ignore-config",
        "--no-warnings",
        "-I",
        "1:%d" % limit,
        "ytsearch%d:%s" % (limit, q),
    ]
    try:
        done = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "YouTube's search did not answer within %d seconds" % TIMEOUT
        )

    found = []
    for line in (done.stdout or b"").decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            found.append(json.loads(line))
        except ValueError:
            continue
    if found:
        return found

    # Nothing parsed. Say what yt-dlp said, because "no results" and "we could
    # not ask" are different things and only one of them is worth a retry.
    detail = (done.stderr or b"").decode("utf-8", "replace").strip().splitlines()
    raise RuntimeError(
        "YouTube search failed: " + (detail[-1] if detail else "no output")
    )


def _subtitle(video):
    """Duration and channel, as the row under the title."""
    parts = []
    seconds = video.get("duration")
    if isinstance(seconds, (int, float)) and seconds > 0:
        parts.append(_duration(int(seconds)))
    uploader = video.get("uploader") or video.get("channel")
    if uploader:
        parts.append(uploader)
    return " · ".join(parts) if parts else None


def _duration(seconds):
    """Seconds as m:ss or h:mm:ss, the way a video is described."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%d:%02d:%02d" % (hours, minutes, secs)
    return "%d:%02d" % (minutes, secs)

"""
YouTube videos, by way of /r/youtubevideos.

The channel this replaces reached for two things. One was a sitewide listing of
every post on Reddit linking to youtube.com; the other was YouTube's own Data
API, with a key written into the file. Neither is here.

The sitewide listing is the reason the channel was retired. The archive this
reads from can search within a named subreddit and nothing wider -- "anywhere on
Reddit" is the one question it cannot be asked -- so that feed had no honest
equivalent. A subreddit is a narrower answer, and it is a real one, which is a
different thing from a feed that answers 200 and is not what it claims to be.

The API key is gone rather than repointed. It was a credential belonging to
whoever published it, committed to a public repository, and Google's terms do
not allow that. Nothing here needs it: the posts carry their own titles,
thumbnails and links, which is what lib.chanutils.reddit already reads.

What is left is two feeds, and neither is a rating. r/youtubevideos does not get
upvoted -- measured back to 2024, every one of a hundred posts scores 1, so
every one is tied -- and a "Top Rated" feed over that would rank a constant and
mean nothing. The subreddit posts about thirty times a day, so the newest and
the hottest are both worth having.
"""

import re

from lib.chanutils.reddit import get_feed
from lib.chanutils.reddit import search as reddit_search
from lib.playitem import PlayItemList

_SUBREDDIT = "youtubevideos"

_FEEDLIST = [
    {"title": "Hot", "url": "http://www.reddit.com/r/youtubevideos.json"},
    {"title": "New", "url": "http://www.reddit.com/r/youtubevideos/new.json"},
]

#: A link to a channel rather than to a video: youtube.com/@handle, and the
#: older /channel/, /c/ and /user/ forms.
#:
#: Three posts in a hundred here are one of these, and the subreddit posts them
#: for a reason -- "we are live", "new video out" -- so they are not rare enough
#: to leave alone. yt-dlp answers a channel URL by walking the channel's whole
#: video list, which is minutes of work it does not finish, and nothing here is
#: waiting for it: the player reads yt-dlp's output in a loop with no timeout,
#: so an item like this does not fail and does not start. It sits there.
#:
#: A video link is fine whichever form it takes. youtu.be, /watch?v=, /shorts/,
#: /embed/ and a playlist all resolve, and a playlist resolves to its first
#: entry because the player passes --no-playlist.
_CHANNEL_URL = re.compile(
    r"^https?://(?:[\w-]+\.)*youtube\.com/(?:@[\w.-]+|channel/|c/|user/)"
)


def _only_videos(items):
    """
    The posts that name one video, without the ones naming a channel.

    Three posts in a hundred. Everything else in this subreddit is playable, and
    dropped rows are dropped before they reach the interface rather than after,
    because an item that cannot play cannot be explained once it is on screen
    looking as playable as the rest.

    A row with no URL is dropped for the same reason. The archive always gives
    one, so it is not a case that arises here -- but this is the one place where
    "cannot play" is decided, and a decision that quietly passes through
    something it cannot handle is the shape of the bug this whole filter is
    here to avoid.
    """
    kept = PlayItemList()
    for item in items.to_list():
        if item.url and not _CHANNEL_URL.match(item.url):
            kept.add(item)
    return kept


def name():
    return "YouTube"


def image():
    return "icon.png"


def description():
    return "YouTube videos posted to /r/youtubevideos (<a target='_blank' href='http://www.reddit.com/r/youtubevideos/'>http://www.reddit.com/r/youtubevideos</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return _only_videos(get_feed(_FEEDLIST[idx]))


def search(q):
    return _only_videos(reddit_search(_SUBREDDIT, q))
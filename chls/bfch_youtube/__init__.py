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

What is left is two feeds: New and Top, which are the two sorts the archive can
be asked for honestly. r/youtubevideos does not get upvoted -- measured back to
2024, every one of a hundred posts scores 1 -- so Top here ranks a constant and
comes back in time order. It is kept anyway, because it is a real ranking of
whatever scores exist rather than a label over something else, and dropping it
for this subreddit alone would be a claim about the subreddit that is not true.
New is the feed to use here: the subreddit posts about thirty times a day.

The filtering of what can be played is not here. Three posts in a hundred link
a channel rather than a video, and yt-dlp never finishes walking one; that
belongs to lib.chanutils.reddit, which every Reddit channel goes through, so
that a channel added later gets it by existing rather than by remembering.
"""

from lib.chanutils.reddit import get_feed
from lib.chanutils.reddit import search as reddit_search

_SUBREDDIT = "youtubevideos"

_FEEDLIST = [
    # Reddit offers five sorts and this archive answers by time and nothing
    # else: 'sort' must be one of asc, desc. These are the two that can be
    # honest. New is newest first, which is what the archive does. Top is
    # ranked by score here, over a window the archive hands back in time order.
    #
    # There is no Hot, Best or Rising. Those are Reddit's own ranking
    # functions -- a score with time decay folded in -- and nothing in the
    # archive computes one. Under those names a feed would be time order
    # wearing a label that promises a ranking, which is exactly what "Hot"
    # already was on every channel: the same first item as the feed beside it.
    # See lib.chanutils.reddit._ranked_by_score for what Top does instead.
    {"title": "New", "url": "http://www.reddit.com/r/youtubevideos/new.json"},
    {"title": "Top", "url": "http://www.reddit.com/r/youtubevideos/top.json"},
]


def name():
    return "YouTube"


def image():
    return "icon.png"


def description():
    return "YouTube videos posted to /r/youtubevideos (<a target='_blank' href='http://www.reddit.com/r/youtubevideos/'>http://www.reddit.com/r/youtubevideos</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return get_feed(_FEEDLIST[idx])


def search(q):
    return reddit_search(_SUBREDDIT, q)
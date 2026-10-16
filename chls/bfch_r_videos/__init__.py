from lib.chanutils.reddit import get_feed
from lib.chanutils.reddit import search as reddit_search

_SUBREDDIT = "videos"

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
    {"title": "New", "url": "http://www.reddit.com/r/videos/new.json"},
    {"title": "Top", "url": "http://www.reddit.com/r/videos/top.json"},
]


def name():
    return "Reddit Videos"


def image():
    return "icon.png"


def description():
    return "All the latest videos from /r/videos subreddit (<a target='_blank' href='http://www.reddit.com/r/videos'>http://www.reddit.com/r/videos</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return get_feed(_FEEDLIST[idx])


def search(q):
    return reddit_search(_SUBREDDIT, q)

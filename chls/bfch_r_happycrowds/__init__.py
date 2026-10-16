import lib.chanutils.reddit

_SUBREDDIT = "happycrowds"

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
    {"title": "New", "url": "http://www.reddit.com/r/happycrowds/new.json"},
    {"title": "Top", "url": "http://www.reddit.com/r/happycrowds/top.json"},
]


def name():
    return "Happy Crowds"


def image():
    return "icon.png"


def description():
    return "Happy crowd videos from /r/happycrowds subreddit (<a target='_blank' href='http://www.reddit.com/r/happycrowds'>http://www.reddit.com/r/happycrowds</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return lib.chanutils.reddit.get_feed(_FEEDLIST[idx])


def search(q):
    return lib.chanutils.reddit.search(_SUBREDDIT, q)

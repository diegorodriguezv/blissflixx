import lib.chanutils.reddit

_SUBREDDIT = "trailers"

_FEEDLIST = [
    {"title": "Hot", "url": "http://www.reddit.com/r/trailers.json"},
    {"title": "Top This Week", "url": "http://www.reddit.com/r/trailers/top.json"},
]


def name():
    return "Movie Trailers"


def image():
    return "icon.png"


def description():
    return "All the latest movie trailers from /r/trailers subreddit (<a target='_blank' href='http://www.reddit.com/r/trailers'>http://www.reddit.com/r/trailers</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    if idx > 0:
        return lib.chanutils.reddit.top(_SUBREDDIT, days=7)
    return lib.chanutils.reddit.get_feed(_FEEDLIST[idx])


def search(q):
    return lib.chanutils.reddit.search(_SUBREDDIT, q)

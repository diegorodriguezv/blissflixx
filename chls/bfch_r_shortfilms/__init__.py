import lib.chanutils.reddit

_SUBREDDIT = "Shortfilms"

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
    {"title": "New", "url": "http://www.reddit.com/r/Shortfilms/new.json"},
    {"title": "Top", "url": "http://www.reddit.com/r/Shortfilms/top.json"},
    {
        "title": "Action & Adventure",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=action%20adventure&sort=top&restrict_sr=on",
    },
    {
        "title": "Animation",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=animation&sort=top&restrict_sr=on",
    },
    {
        "title": "Art Films",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=art%20films&sort=top&restrict_sr=on",
    },
    {
        "title": "Comedy",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=comedy&sort=top&restrict_sr=on",
    },
    {
        "title": "Crime",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=crime&sort=top&restrict_sr=on",
    },
    {
        "title": "Documentary",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=documentary&sort=top&restrict_sr=on",
    },
    {
        "title": "Drama",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=drama&sort=top&restrict_sr=on",
    },
    {
        "title": "Experimental",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=experimental&sort=top&restrict_sr=on",
    },
    {
        "title": "Film Noir",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=film%20noir&sort=top&restrict_sr=on",
    },
    {
        "title": "Gay & Lesbian",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=gay&sort=top&restrict_sr=on",
    },
    {
        "title": "Horror",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=horror&sort=top&restrict_sr=on",
    },
    {
        "title": "Musical",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=musical&sort=top&restrict_sr=on",
    },
    {
        "title": "Mystery",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=mystery&sort=top&restrict_sr=on",
    },
    {
        "title": "Parody",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=parody&sort=top&restrict_sr=on",
    },
    {
        "title": "Romance",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=romance&sort=top&restrict_sr=on",
    },
    {
        "title": "Sci-Fi & Fantasy",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=sci-fi%20fantasy&sort=top&restrict_sr=on",
    },
    {
        "title": "Surreal",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=surreal&sort=top&restrict_sr=on",
    },
    {
        "title": "Thriller",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=thriller&sort=top&restrict_sr=on",
    },
    {
        "title": "War",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=war&sort=top&restrict_sr=on",
    },
    {
        "title": "Western",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=western&sort=top&restrict_sr=on",
    },
    {
        "title": "World Cinema",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=world%20cinema&sort=top&restrict_sr=on",
    },
    {
        "title": "Amateur",
        "url": "http://www.reddit.com/r/Shortfilms/search.json?q=amateur&sort=top&restrict_sr=on",
    },
]


def name():
    return "Short Films"


def image():
    return "icon.png"


def description():
    return "Short Films from /r/Shortfilms subreddit (<a target='_blank' href='http://www.reddit.com/r/Shortfilms'>http://www.reddit.com/r/Shortfilms</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return lib.chanutils.reddit.get_feed(_FEEDLIST[idx])


def search(q):
    return lib.chanutils.reddit.search(_SUBREDDIT, q)

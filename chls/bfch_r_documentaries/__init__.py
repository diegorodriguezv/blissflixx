import lib.chanutils.reddit

_SUBREDDIT = "Documentaries"

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
    {"title": "New", "url": "http://www.reddit.com/r/Documentaries/new.json"},
    {"title": "Top", "url": "http://www.reddit.com/r/Documentaries/top.json"},
    {
        "title": "Anthropology",
        "url": "http://www.reddit.com/r/documentaries/search.json?q=flair%3A%27Anthropology%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Art",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Art%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Biography",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Biography%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Crime",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Crime%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Cusine",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Cuisine%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Disaster",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Disaster%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Drugs",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Drugs%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Economics",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Economics%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "History",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27History%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "History (Ancient)",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Ancient+history%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "History (20th Century)",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%2720th+century%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Intelligence",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Intelligence%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Literature",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Literature%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Medicine",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Medicine%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Music",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Music%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Nature",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Nature%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Offbeat",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Offbeat%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "American Politics",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27American+politics%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "International Politics",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Int+politics%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Psychology",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Psychology%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Religion",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Religion%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Science",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Science%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Sex",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Sex%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Sport",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Sport%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Tech/Internet",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Tech%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Travel",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Travel%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "War",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27War%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "World War 1",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27WW1%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "World War 2",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27WW2%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Vietnam War",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Vietnam+war%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Afghanistan War",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Afghanistan+conflict%27&sort=top&restrict_sr=on&t=all",
    },
    {
        "title": "Iraq War",
        "url": "http://www.reddit.com/r/Documentaries/search.json?q=flair%3A%27Iraq+conflict%27&sort=top&restrict_sr=on&t=all",
    },
]


def name():
    return "Documentaries"


def image():
    return "icon.png"


def description():
    return "Assorted Documentaries Channel for /r/Documentaries subreddit (<a target='_blank' href='http://www.reddit.com/r/Documentaries'>http://www.reddit.com/r/Documentaries</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    return lib.chanutils.reddit.get_feed(_FEEDLIST[idx])


def search(q):
    return lib.chanutils.reddit.search(_SUBREDDIT, q)

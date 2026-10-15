import lxml.html

from lib.chanutils import get
from lib.playitem import PlayItem, PlayItemList

_FEEDLIST = [
    {
        "title": "Ads and Commercials",
        "url": "https://vimeo.com/categories/adsandcommercials/videos",
    },
    {"title": "Animation", "url": "https://vimeo.com/categories/animation/videos"},
    {
        "title": "Branded Content",
        "url": "https://vimeo.com/categories/brandedcontent/videos",
    },
    {"title": "Comedy", "url": "http://vimeo.com/categories/comedy/videos"},
    {"title": "Documentary", "url": "http://vimeo.com/categories/documentary/videos"},
    {"title": "Experimental", "url": "http://vimeo.com/categories/experimental/videos"},
    {"title": "Music", "url": "http://vimeo.com/categories/music/videos"},
    {"title": "Narrative", "url": "http://vimeo.com/categories/narrative/videos"},
    {"title": "Sports", "url": "https://vimeo.com/categories/sports/videos"},
    {"title": "Travel", "url": "https://vimeo.com/categories/travel/videos"},
]


def name():
    return "Vimeo"


def image():
    return "icon.png"


def description():
    return "Vimeo Channel (<a target='_blank' href='https://vimeo.com/'>https://vimeo.com/</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    url = _FEEDLIST[idx]["url"]
    r = get(url)
    return _extract(r.text)


# No search(). Vimeo blocks it.
#
# The channel used to offer one, pointed at https://vimeo.com/search, and it
# failed every time: vimeo.com/search answers 403 to everything, and that is not
# a rate limit or a user-agent problem. Measured, all of these are 403 --
#
#     /search?q=the            /search?type=videos&q=the
#     /search?q=the&type=videos        the same with a browser User-Agent
#
# -- while the category pages this channel actually reads answer 200 and parse.
# So the categories work and the search does not, and there is no variant of the
# URL that changes it.
#
# Leaving the function out is what stops the interface offering the box. A module
# with search() gets a search field; without one it gets none, and the channel
# still has its ten categories. What it had instead was a search box that could
# only ever produce a 500 and an error page -- which is worse than no box, because
# it looked like the channel had a search and the search was broken.


def _extract(text):
    doc = lxml.html.fromstring(text)
    results = PlayItemList()
    for li in doc.xpath("//ol/li"):
        anchors = li.xpath(".//a")
        anchor = None
        for a in anchors:
            href = a.get("href") or ""
            if href.lstrip("/").isdigit():
                anchor = a
                break
        if anchor is None:
            continue
        title = anchor.get("title")
        if not title:
            title = anchor.text_content().strip()
        img_el = li.xpath(".//img")
        img = img_el[0].get("src") if img_el else None
        href = anchor.get("href")
        url = "https://vimeo.com" + href
        results.add(PlayItem(title, img, url))
    return results

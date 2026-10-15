from lib.chanutils import get_attr, get_doc, get_text, select_all, select_one
from lib.playitem import PlayItem, PlayItemList

# "TCTV News" used to be here and is not any more.
#
# Every show on this list is written as /shows/<slug>/ and redirects to
# /collection/<slug>/. Eighteen of them resolve; TCTV News redirects to
# /collection/tctv-news, which is a 404. The show page has been taken off the
# site, and its 404 body is a full page that happens to carry seventeen
# loop-card elements -- so a selector count says "17 items found" on a page that
# does not exist. Only the status code tells the truth, which is why this was
# found by asking the server rather than by counting cards.
#
# Its videos do still exist, in the WordPress API, but there is no page to point
# a feed at. The obvious repairs were both worse than removal:
#
#   /video/?s=tctv                     200, twelve cards -- and the term is
#                                      ignored, so it is a generic recent-
#                                      videos listing, i.e. "All Videos" again
#   /?s=tctv+news&post_type=tc_video   200, eight cards -- also unfiltered
#
# Both answer 200 and would have looked fine while quietly showing something else
# entirely, under a name promising TCTV News. A feed that lies about its
# contents is worse than a feed that is not there.
_FEEDLIST = [
    {"title": "All Videos", "url": "http://techcrunch.com/video/"},
    {"title": "TC Gadgets", "url": "http://techcrunch.com/shows/gadgets/"},
    {"title": "Features", "url": "http://techcrunch.com/shows/features/"},
    {"title": "Reviews", "url": "http://techcrunch.com/shows/reviews/"},
    {"title": "TC Interviews", "url": "http://techcrunch.com/shows/interviews/"},
    {"title": "Apps", "url": "http://techcrunch.com/shows/apps/"},
    {"title": "Disrupt", "url": "http://techcrunch.com/shows/techcrunch-disrupt/"},
    {
        "title": "Battlefield",
        "url": "http://techcrunch.com/shows/techcrunch-battlefield/",
    },
    {"title": "Sessions", "url": "http://techcrunch.com/shows/tc-sessions/"},
    {"title": "Crunch Reports", "url": "http://techcrunch.com/shows/crunch-report/"},
    {
        "title": "Judah vs the Machines",
        "url": "http://techcrunch.com/shows/judah-vs-the-machines/",
    },
    {"title": "Down Round", "url": "http://techcrunch.com/shows/down-round/"},
    {"title": "Trust Disrupted", "url": "http://techcrunch.com/shows/trust-disrupted/"},
    {
        "title": "Built in Brooklyn",
        "url": "http://techcrunch.com/shows/built-in-brooklyn/",
    },
    {"title": "TC Cribs", "url": "http://techcrunch.com/shows/tc-cribs/"},
    {"title": "Bullish", "url": "http://techcrunch.com/shows/bullish/"},
    {"title": "CrunchWeek", "url": "http://techcrunch.com/shows/crunchweek/"},
    {"title": "Fly or Die", "url": "http://techcrunch.com/shows/fly-or-die/"},
]


def name():
    return "Tech Crunch"


def image():
    return "icon.png"


def description():
    return "Tech Crunch (<a target='_blank' href='https://techcrunch.com'>https://techcrunch.com/a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    doc = get_doc(_FEEDLIST[idx]["url"])
    rtree = select_all(doc, "div.loop-card")
    if not rtree:
        rtree = select_all(doc, "div.post-block")
    results = PlayItemList()
    for l in rtree:
        loop_title = select_one(l, "a.loop-card__title-link")
        if loop_title is not None:
            url = get_attr(loop_title, "href")
            title = get_text(loop_title)
            img_el = select_one(l, "img")
            img = get_attr(img_el, "src")
            content_el = select_one(l, "div.loop-card__content")
            subtitle = get_text(content_el)
        else:
            url_el = select_one(l, "a")
            url = get_attr(url_el, "href")
            img_el = select_one(l, "img")
            img = get_attr(img_el, "src")
            content_el = select_one(l, "div.post-block__content")
            subtitle = get_text(content_el)
            title_el = select_one(l, "a.post-block__title__link")
            title = get_text(title_el)
        results.add(PlayItem(title, img, url, subtitle))
    return results

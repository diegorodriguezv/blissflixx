from chanutils import (
    get_doc,
    select_all,
    select_one,
    get_attr,
    get_text,
    get_text_content,
)
from playitem import PlayItem, PlayItemList, MoreEpisodesAction

_SEARCH_URL = "http://www.bbc.co.uk/iplayer/search"

_FEEDLIST = [
    {"title": "Most Popular", "url": "https://www.bbc.co.uk/iplayer/most-popular"},
    {
        "title": "Arts",
        "url": "http://www.bbc.co.uk/iplayer/categories/arts/all?sort=dateavailable",
    },
    {
        "title": "CBBC",
        "url": "http://www.bbc.co.uk/iplayer/categories/cbbc/all?sort=dateavailable",
    },
    {
        "title": "CBeebies",
        "url": "http://www.bbc.co.uk/iplayer/categories/cbeebies/all?sort=dateavailable",
    },
    {
        "title": "Comedy",
        "url": "http://www.bbc.co.uk/iplayer/categories/comedy/all?sort=dateavailable",
    },
    {
        "title": "Documentaries",
        "url": "http://www.bbc.co.uk/iplayer/categories/documentaries/all?sort=dateavailable",
    },
    {
        "title": "Drama & Soaps",
        "url": "http://www.bbc.co.uk/iplayer/categories/drama-and-soaps/all?sort=dateavailable",
    },
    {
        "title": "Entertainment",
        "url": "http://www.bbc.co.uk/iplayer/categories/entertainment/all?sort=dateavailable",
    },
    {
        "title": "Films",
        "url": "http://www.bbc.co.uk/iplayer/categories/films/all?sort=dateavailable",
    },
    {
        "title": "Food",
        "url": "http://www.bbc.co.uk/iplayer/categories/food/all?sort=dateavailable",
    },
    {
        "title": "History",
        "url": "http://www.bbc.co.uk/iplayer/categories/history/all?sort=dateavailable",
    },
    {
        "title": "Lifestyle",
        "url": "http://www.bbc.co.uk/iplayer/categories/lifestyle/all?sort=dateavailable",
    },
    {
        "title": "Music",
        "url": "http://www.bbc.co.uk/iplayer/categories/music/all?sort=dateavailable",
    },
    {
        "title": "News",
        "url": "http://www.bbc.co.uk/iplayer/categories/news/all?sort=dateavailable",
    },
    {
        "title": "Science & Nature",
        "url": "http://www.bbc.co.uk/iplayer/categories/science-and-nature/all?sort=dateavailable",
    },
    {
        "title": "Sport",
        "url": "http://www.bbc.co.uk/iplayer/categories/sport/all?sort=dateavailable",
    },
]


def name():
    return "BBC iPlayer"


def image():
    return "icon.png"


def description():
    return "BBC iPlayer Channel (<a target='_blank' href='http://www.bbc.co.uk/iplayer'>http://www.bbc.co.uk/iplayer</a>). Geo-restricted to UK."


def feedlist():
    return _FEEDLIST


def feed(idx):
    doc = get_doc(_FEEDLIST[idx]["url"])
    return _extract_grid(doc)


def search(q):
    doc = get_doc(_SEARCH_URL, params={"q": q})
    return _extract_grid(doc)


def showmore(link):
    doc = get_doc(link)
    return _extract_grid(doc)


def _extract_grid(doc):
    rtree = select_all(doc, "li.grid__item")
    results = PlayItemList()
    for l in rtree:
        a = select_one(l, "a")
        url = get_attr(a, "href")
        if url is None:
            continue
        if url.startswith("/iplayer"):
            url = "http://www.bbc.co.uk" + url
        idiv = select_one(l, "div.rs-image")
        idiv = select_one(idiv, "source")
        img = get_attr(idiv, "srcset").split()[0]

        sdiv = select_one(l, "div.content-item__info__text")
        avail = select_one(sdiv, "div.content-item__labels")
        if get_text_content(avail) == "Not available":
            continue

        title = get_text_content(select_one(sdiv, "div.content-item__title"))
        subtitle = get_text_content(select_one(sdiv, "div.content-item__description"))
        if title.endswith("..."):
            title = title[:-3]
        if subtitle.endswith("..."):
            subtitle = subtitle[:-3]

        item = PlayItem(title, img, url, subtitle)
        a = select_one(l, "a.js-view-all-episodes")
        if a is not None:
            link = "http://bbc.co.uk" + a.get("href")
            item.add_action(MoreEpisodesAction(link, title))
        results.add(item)
    return results


def _extract(doc):
    rtree = select_all(doc, "li.list-item")
    results = PlayItemList()
    for l in rtree:
        a = select_one(l, "a")
        url = get_attr(a, "href")
        if url is None:
            continue
        if url.startswith("/iplayer"):
            url = "http://www.bbc.co.uk" + url

        pdiv = select_one(l, "div.primary")
        idiv = select_one(pdiv, "div.r-image")
        if idiv is None:
            idiv = select_one(pdiv, "div.rs-image")
            idiv = select_one(idiv, "source")
            img = get_attr(idiv, "srcset")
        else:
            img = get_attr(idiv, "data-ip-src")

        sdiv = select_one(l, "div.secondary")
        title = get_text(select_one(sdiv, "div.title"))
        subtitle = get_text(select_one(sdiv, "div.subtitle"))
        synopsis = get_text(select_one(sdiv, "p.synopsis"))
        item = PlayItem(title, img, url, subtitle, synopsis)
        a = select_one(l, "a.view-more-container")
        if a is not None:
            link = "http://bbc.co.uk" + a.get("href")
            item.add_action(MoreEpisodesAction(link, title))
        results.add(item)
    return results

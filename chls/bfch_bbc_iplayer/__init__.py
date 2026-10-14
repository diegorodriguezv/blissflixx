from lib.chanutils import (
    get_attr,
    get_doc,
    get_text,
    get_text_content,
    select_all,
    select_one,
)
from lib.playitem import MoreEpisodesAction, PlayItem, PlayItemList

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
        img = None
        idiv = select_one(l, "div.rs-image")
        if idiv is not None:
            for _candidate in (
                select_one(idiv, "img.rs-image__img"),
                select_one(idiv, "img"),
                select_one(idiv, "source"),
            ):
                if _candidate is None:
                    continue
                srcset = get_attr(_candidate, "srcset")
                if srcset:
                    img = str(srcset).split()[0]
                    break
                src = get_attr(_candidate, "src")
                if src:
                    img = src
                    break
                data_src = get_attr(_candidate, "data-src")
                if data_src:
                    img = data_src
                    break
        if not img:
            for _img in l.xpath(".//img"):
                srcset = _img.get("srcset")
                if srcset:
                    img = str(srcset).split()[0]
                    break
                src = _img.get("src")
                if src and not str(src).startswith("data:"):
                    img = src
                    break
        if not img:
            _raw = get_attr(a, "data-v-item")
            if _raw:
                try:
                    import json as _json

                    _meta = _json.loads(_raw)
                    _tmpl = _meta.get("image")
                    if isinstance(_tmpl, str) and "{recipe}" in _tmpl:
                        img = _tmpl.replace("{recipe}", "304x171")
                except (ValueError, TypeError):
                    pass

        sdiv = select_one(l, "div.content-item__info__text")
        avail = select_one(sdiv, "div.content-item__labels") if sdiv is not None else None
        if get_text_content(avail) == "Not available":
            continue
        if any(get_text_content(_el) == "Not available" for _el in l.iter()):
            continue

        title = None
        subtitle = None
        if sdiv is not None:
            title = get_text_content(select_one(sdiv, "div.content-item__title"))
            subtitle = get_text_content(select_one(sdiv, "div.content-item__description"))
        if not title:
            for _expr in (
                "div.content-item-root__title",
                "div.content-item-root__meta",
                "div.content-item__title",
            ):
                title = get_text_content(select_one(l, _expr))
                if title:
                    break
        if not title:
            _aria = get_attr(a, "aria-label")
            if _aria:
                _parts = [p.strip() for p in str(_aria).split(".") if p.strip()]
                if len(_parts) > 1 and _parts[0].lower().startswith(
                    ("1 of ", "2 of ", "3 of ", "4 of ", "5 of ")
                ):
                    title = _parts[1]
                elif _parts:
                    title = _parts[0]
        if not subtitle:
            _aria = get_attr(a, "aria-label")
            if _aria and "Description:" in _aria:
                subtitle = _aria.split("Description:", 1)[1].strip()
                for _marker in (" Category:", " Duration:", " Rating:"):
                    if _marker in subtitle:
                        subtitle = subtitle.split(_marker, 1)[0].strip()
        if not title:
            continue
        if subtitle is None:
            subtitle = ""
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

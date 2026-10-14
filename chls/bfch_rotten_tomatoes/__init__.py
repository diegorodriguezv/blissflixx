import json

import lxml.html

from lib.chanutils import get
from lib.playitem import PlayItem, PlayItemList

_FEEDLIST = [
    {
        "title": "Top Rentals",
        "url": "https://www.rottentomatoes.com/browse/movies_at_home/sort:popular",
    },
    {
        "title": "Top Rated",
        "url": "https://www.rottentomatoes.com/browse/movies_at_home/critics:certified_fresh",
    },
    {
        "title": "New Releases",
        "url": "https://www.rottentomatoes.com/browse/movies_at_home/sort:newest",
    },
]


def name():
    return "Rotten Tomatoes"


def image():
    return "icon.png"


def description():
    return "Rotten Tomatoes Channel (<a target='_blank' href='https://www.rottentomatoes.com/'>https://www.rottentomatoes.com/</a>)."


def feedlist():
    return _FEEDLIST


def feed(idx):
    r = get(_FEEDLIST[idx]["url"])
    return _extract(r.text)


def _movies_from_data(data):
    movies = []
    item_list = data.get("itemListElement") if isinstance(data, dict) else None
    if isinstance(item_list, dict):
        nested = item_list.get("itemListElement")
        if isinstance(nested, list):
            movies = nested
        elif isinstance(nested, dict):
            movies = [nested]
    elif isinstance(item_list, list):
        movies = item_list

    if not movies:
        # Fall back to a recursive search for movie entries.
        stack = [data]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                if current.get("@type") == "Movie":
                    movies.append(current)
                for value in current.values():
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
    return movies


def _extract(text):
    results = PlayItemList()
    doc = lxml.html.fromstring(text)
    for script in doc.xpath('//script[@type="application/ld+json"]'):
        try:
            data = json.loads(script.text_content())
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        for item in _movies_from_data(data):
            if not isinstance(item, dict) or item.get("@type") != "Movie":
                continue
            name = item.get("name")
            image = item.get("image")
            url = item.get("url")
            subtitle = item.get("dateCreated")
            synopsis = None
            aggregate_rating = item.get("aggregateRating")
            if isinstance(aggregate_rating, dict):
                synopsis = aggregate_rating.get("ratingValue")
            results.add(PlayItem(name, image, url, subtitle, synopsis))
    return results
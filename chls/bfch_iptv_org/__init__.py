import time

from lib.chanutils import get_json
from lib.playitem import PlayItem, PlayItemList

_API = {
    "channels": "https://iptv-org.github.io/api/channels.json",
    "streams": "https://iptv-org.github.io/api/streams.json",
    "logos": "https://iptv-org.github.io/api/logos.json",
    "guides": "https://iptv-org.github.io/api/guides.json",
    "categories": "https://iptv-org.github.io/api/categories.json",
    "languages": "https://iptv-org.github.io/api/languages.json",
    "countries": "https://iptv-org.github.io/api/countries.json",
}

# The upstream payloads are static but large (channels ~8MB, logos ~6MB,
# guides ~26MB), and every feed/search call needs them, so keep both the
# downloaded payloads and the indexes derived from them around for a while
# instead of refetching and re-parsing on each request.
_CACHE = {}
_CACHE_TTL = 60 * 60


def _cached(key, builder):
    entry = _CACHE.get(key)
    if entry and time.monotonic() - entry[0] < _CACHE_TTL:
        return entry[1]
    data = builder()
    _CACHE[key] = (time.monotonic(), data)
    return data


def _get_cached(key):
    return _cached(key, lambda: get_json(_API[key]))


_FEEDLIST = [
    {"title": "Colombia", "filter": {"country": "CO"}},
    {"title": "Great Britain", "filter": {"country": "UK"}},
    {"title": "China", "filter": {"country": "CN"}},
    {"title": "India", "filter": {"country": "IN"}},
    {"title": "Indonesia", "filter": {"country": "ID"}},
    {"title": "Pakistan", "filter": {"country": "PK"}},
    {"title": "Nigeria", "filter": {"country": "NG"}},
    {"title": "Brazil", "filter": {"country": "BR"}},
    {"title": "Bangladesh", "filter": {"country": "BD"}},
    {"title": "Russia", "filter": {"country": "RU"}},
    {"title": "Mexico", "filter": {"country": "MX"}},
    {"title": "United States", "filter": {"country": "US"}},
    {"title": "Spanish", "filter": {"language": "es"}},
    {"title": "English", "filter": {"language": "en"}},
    {"title": "French", "filter": {"language": "fr"}},
    {"title": "German", "filter": {"language": "de"}},
    {"title": "Japanese", "filter": {"language": "ja"}},
    {"title": "Portuguese", "filter": {"language": "pt"}},
    {"title": "NSFW", "filter": {"nsfw": True}},
]
_MAXRESULTS = 300


def name():
    return "/iptv-org"


def image():
    return "icon.png"


def description():
    return "Collection of publicly available IPTV channels from all over the world (<a target='_blank' href='https://iptv-org.github.io/'>https://iptv-org.github.io/</a>). IPTV = Internet Protocol TeleVision."


def feedlist():
    return _FEEDLIST


def _build_logo_index():
    """logos.json maps channel id -> logo entries; some channels have several."""
    index = {}
    for logo in _get_cached("logos"):
        chid = logo.get("channel")
        url = logo.get("url")
        if not chid or not url:
            continue
        current = index.get(chid)
        # prefer logos the project actually ships, then raster over vector,
        # then the highest resolution
        rank = (
            bool(logo.get("in_use")),
            logo.get("format") != "SVG",
            logo.get("width") or 0,
        )
        if current is None or rank > current[0]:
            index[chid] = (rank, url)
    return {chid: url for chid, (_, url) in index.items()}


def _build_language_index():
    """guides.json is the only remaining source of per-channel language."""
    index = {}
    for guide in _get_cached("guides"):
        chid = guide.get("channel")
        lang = guide.get("lang")
        if not chid or not lang:
            continue
        index.setdefault(chid, set()).add(lang)
    return index


def load_all_streams():
    """link each of the streams with it's corresponding channel"""
    streams = _get_cached("streams")
    channels = _get_cached("channels")
    logos = _cached("logo_index", _build_logo_index)
    languages = _cached("language_index", _build_language_index)
    channels_idx = {ch["id"]: ch for ch in channels}
    num_channels_added = 0
    for stream in streams:
        if stream["channel"] and stream["channel"] in channels_idx:
            info = channels_idx[stream["channel"]]
            # these two fields were dropped from channels.json upstream, so
            # re-attach them from the sources that still carry them
            info["logo"] = logos.get(stream["channel"])
            info["languages"] = languages.get(stream["channel"], set())
            stream["channel_info"] = info
            num_channels_added += 1
    return streams


def feed(idx):
    streams = load_all_streams()
    filter = _FEEDLIST[idx]["filter"]
    data = []
    if "country" in filter:
        data = [
            s
            for s in streams
            if "channel_info" in s and s["channel_info"]["country"] == filter["country"]
        ]
    elif "language" in filter:
        data = [
            s
            for s in streams
            if "channel_info" in s
            and filter["language"] in s["channel_info"]["languages"]
        ]
    elif "nsfw" in filter:
        data = [
            s for s in streams if "channel_info" in s and s["channel_info"]["is_nsfw"]
        ]
    return _extract(data[:_MAXRESULTS])


def search(q):
    streams = load_all_streams()
    result = [
        s
        for s in streams
        if "channel_info" in s and q.lower() in s["channel_info"]["name"].lower()
    ]
    return _extract(result[:_MAXRESULTS])


def _extract(data):
    results = PlayItemList()
    for stream in data:
        info = stream["channel_info"]
        title = info["name"]
        thumb = info.get("logo")
        url = stream["url"]
        subtitle_strings = []
        if stream.get("quality"):
            subtitle_strings.append(f"quality: <b>{stream['quality']}</b>")
        if stream.get("labels"):
            subtitle_strings.append(f"labels: <b>{' '.join(stream['labels'])}</b>")
        if "country" in info:
            subtitle_strings.append(f"country: <b>{info['country']}</b>")
        if info.get("languages"):
            subtitle_strings.append(
                f"languages: <b>{' '.join(sorted(info['languages']))}</b>"
            )
        if "categories" in info and info["categories"]:
            subtitle_strings.append(
                f"categories: <b>{' '.join(info['categories'])}</b>"
            )
        subtitle = " - ".join(subtitle_strings)
        item = PlayItem(title, thumb, url, subtitle)
        results.add(item)
    return results

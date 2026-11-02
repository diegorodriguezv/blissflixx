import time

from lib import playquality
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

# The upstream payloads are static but large, and every feed/search call needs
# them, so keep both the downloaded payloads and the indexes derived from them
# around for a while instead of refetching and re-parsing on each request.
_CACHE = {}
_CACHE_TTL = 60 * 60

# guides.json is ~26MB and takes far longer than the default request timeout to
# pull down on a slow link, so it gets its own generous timeout and is only ever
# fetched for the language feeds.
_GUIDES_TIMEOUT = 300


def _cached(key, builder):
    entry = _CACHE.get(key)
    if entry and time.monotonic() - entry[0] < _CACHE_TTL:
        return entry[1]
    data = builder()
    _CACHE[key] = (time.monotonic(), data)
    return data


def _get_cached(key, **kwargs):
    return _cached(key, lambda: get_json(_API[key], **kwargs))


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
]
_MAXRESULTS = 300

#: Mirrors carried with a row. The player tries them in order when the picture
#: never starts; more than a handful is minutes of waiting rather than a better
#: chance, and the first four have already been ordered by what worked.
_MAX_SOURCES = 4

#: How long a recorded outcome is worth showing. A stream that worked last
#: month says nothing about the URL serving it now.
_RECENT = 7 * 24 * 60 * 60

#: A start slower than this is called slow rather than worked. The count this
#: writes is measured from the attempt, not from the click, so failover does
#: not make a working mirror look slow.
_SLOW_START = 10


def name():
    # No leading slash. It has been here since the channel was first written in
    # 2023 and it is not a convention: of the fifteen channel names this is the
    # only one that starts with anything but a letter, so it reads as a typo
    # that made it onto the menu. Nothing keys off the name -- channels are
    # addressed by chid everywhere -- so dropping it is safe.
    return "iptv-org"


def image():
    return "icon.png"


def description():
    return "Collection of publicly available IPTV channels from all over the world (<a target='_blank' href='https://iptv-org.github.io'>https://iptv-org.github.io</a>). IPTV = Internet Protocol TeleVision."


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
    for guide in _get_cached("guides", timeout=_GUIDES_TIMEOUT):
        chid = guide.get("channel")
        lang = guide.get("lang")
        if not chid or not lang:
            continue
        index.setdefault(chid, set()).add(lang)
    return index


def _language_index():
    return _cached("language_index", _build_language_index)


def load_all_streams():
    """link each of the streams with it's corresponding channel"""
    streams = _get_cached("streams")
    channels = _get_cached("channels")
    logos = _cached("logo_index", _build_logo_index)
    # guides.json is only wanted by the language feeds, and pulling it costs
    # far more than the rest combined, so leave it out unless one is asked for
    languages = _CACHE.get("language_index")
    languages = languages[1] if languages else None
    channels_idx = {ch["id"]: ch for ch in channels}
    num_channels_added = 0
    for stream in streams:
        if stream["channel"] and stream["channel"] in channels_idx:
            info = channels_idx[stream["channel"]]
            # these two fields were dropped from channels.json upstream, so
            # re-attach them from the sources that still carry them
            info["logo"] = logos.get(stream["channel"])
            if languages is not None:
                info["languages"] = languages.get(stream["channel"], set())
            stream["channel_info"] = info
            num_channels_added += 1
    return streams


def feed(idx):
    filter = _FEEDLIST[idx]["filter"]
    # only the language feeds need guides.json, so pay for it only there
    if "language" in filter:
        _language_index()
    streams = load_all_streams()
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
        # Kept because the filter is a general mechanism, not because there is a
        # feed using it. The NSFW feed was removed: the whole world list holds
        # exactly one such stream, and it did not play. A menu entry that lists a
        # single dead stream is worse than not offering it.
        data = [
            s
            for s in streams
            if "channel_info" in s and s["channel_info"].get("is_nsfw")
        ]
    return _extract(data)


def search(q):
    streams = load_all_streams()
    result = [
        s
        for s in streams
        if "channel_info" in s and q.lower() in s["channel_info"]["name"].lower()
    ]
    return _extract(result)


def _resolution(value):
    """The number in a quality string, for ordering. Unknown is the lowest."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return int(digits) if digits else 0


def _outcome(url):
    """
    The last thing this URL did, if it did anything worth remembering.

    The latest event wins: a stream that worked and then failed is not a
    working stream, and one that failed and then worked is. Stale entries are
    ignored rather than deleted here -- the journal prunes itself.
    """
    entry = playquality.summary(url) or {}
    events = []
    if isinstance(entry.get("ok_at"), (int, float)):
        events.append(("ok", entry["ok_at"], float(entry.get("ok_seconds") or 0)))
    if isinstance(entry.get("bad_at"), (int, float)):
        events.append(("bad", entry["bad_at"], 0.0))
    if isinstance(entry.get("froze_at"), (int, float)):
        events.append(("froze", entry["froze_at"], 0.0))
    if not events:
        return None
    kind, at, seconds = max(events, key=lambda event: event[1])
    if time.time() - at > _RECENT:
        return None
    return kind, at, seconds


def _order_streams(streams):
    """
    The mirrors, best first, without removing the ones that failed.

    The order is also the failover order, so it is the same evidence the row
    shows: a recent success first, then the quality of an untested URL, and a
    recent failure last. Within the working ones the freshest goes first --
    whether a URL answered an hour ago says more about the one there now than
    how fast it answered six days ago.
    """

    def key(stream):
        outcome = _outcome(stream["url"])
        labels = stream.get("labels") or []
        discouraged = ("Geo-blocked" in labels) + ("Not 24/7" in labels)
        quality = -_resolution(stream.get("quality"))
        if outcome and outcome[0] == "ok":
            return (0, -outcome[1], outcome[2], discouraged, quality)
        if outcome:
            # The oldest failure first: it has had the most time to be fixed.
            return (2, outcome[1], 0.0, discouraged, quality)
        return (1, 0.0, 0.0, discouraged, quality)

    return sorted(streams, key=key)


def _channel_outcome(streams):
    """The best evidence for the channel: a working mirror if there is one."""
    working = []
    broken = []
    for stream in streams:
        outcome = _outcome(stream["url"])
        if outcome is None:
            continue
        if outcome[0] == "ok":
            working.append(outcome)
        else:
            broken.append(outcome)
    if working:
        return max(working, key=lambda event: event[1])
    if broken:
        return max(broken, key=lambda event: event[1])
    return None


def _ago(when):
    seconds = max(0, int(time.time() - when))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def _mark(streams, info):
    """
    What the row says about how this channel behaved here, if anything.

    A mark is a statement about this box, not about the stream: "failed 10m
    ago" is true even when the channel is fine on somebody else's network.
    """
    outcome = _channel_outcome(streams)
    if outcome and outcome[0] == "ok":
        seconds = max(1, int(round(outcome[2])))
        when = _ago(outcome[1])
        if seconds >= _SLOW_START:
            return "warn", "slow · %ds · %s" % (seconds, when)
        return "ok", "worked · %ds · %s" % (seconds, when)
    if outcome:
        kind, at, _ = outcome
        if kind == "froze":
            return "warn", "froze · %s" % _ago(at)
        return "bad", "failed · %s" % _ago(at)
    if info.get("closed"):
        return "bad", "closed"
    return None


def _channel_rank(streams, info):
    """Where the channel sits in the list: working, unknown, then failed."""
    outcome = _channel_outcome(streams)
    if outcome and outcome[0] == "ok":
        return 0, float(outcome[2])
    if outcome:
        return 2, 0.0
    if info.get("closed"):
        return 2, 0.0
    return 1, 0.0


def _extract(data):
    """
    One row per channel, with its mirrors behind it.

    A feed used to be one row per stream, so Caracol TV was thirty-five
    identical rows with thirty-five different addresses and a dead one looked
    like a dead channel. The mirrors are kept, ordered, and carried with the
    play instead; see lib/player/player.py for the failover.
    """
    groups = {}
    order = []
    for stream in data:
        if "channel_info" not in stream or not stream.get("channel"):
            continue
        chid = stream["channel"]
        if chid not in groups:
            groups[chid] = []
            order.append(chid)
        groups[chid].append(stream)

    items = []
    for position, chid in enumerate(order):
        streams = _order_streams(groups[chid])
        info = streams[0]["channel_info"]
        sources = []
        for stream in streams:
            url = stream.get("url")
            if url and url not in sources:
                sources.append(url)
            if len(sources) >= _MAX_SOURCES:
                break
        if not sources:
            continue
        first = streams[0]
        subtitle_strings = []
        if first.get("quality"):
            subtitle_strings.append(f"quality: <b>{first['quality']}</b>")
        if first.get("labels"):
            subtitle_strings.append(f"labels: <b>{' '.join(first['labels'])}</b>")
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
        mark = _mark(streams, info)
        if mark:
            subtitle_strings.append(
                '<span class="iptv-status %s">%s</span>' % mark
            )
        subtitle = " - ".join(subtitle_strings)
        item = PlayItem(info["name"], info.get("logo"), sources[0], subtitle)
        item.sources = sources
        items.append((_channel_rank(streams, info), position, item))

    items.sort(key=lambda row: (row[0][0], row[0][1], row[1]))
    results = PlayItemList()
    for _rank, _position, item in items[:_MAXRESULTS]:
        results.add(item)
    return results

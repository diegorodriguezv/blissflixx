import datetime
import html
import html.entities
import os
import random
import re
import urllib.error
import urllib.parse

import lxml.html
import requests
from lxml.cssselect import CSSSelector

# The CORS proxy is dead: this App Engine instance dates from 2019 and is long
# gone. Nothing in BlissFlixx passes proxy=True, so this path is never taken.
# It is kept, rather than removed, because get() and post() expose proxy= as
# part of this module's public surface and community channels in plugins/ may
# still pass it. Do not add new callers.
_PROXY_LIST = [{"url": "http://blissflixx-proxy1.appspot.com"}]

# Default network timeout in seconds. Without this a stalled remote host blocks
# a CherryPy request thread indefinitely. Override with BF_TIMEOUT if a slow
# link needs longer.
try:
    _TIMEOUT = float(os.environ.get("BF_TIMEOUT", "20"))
except ValueError:
    _TIMEOUT = 20.0


_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "accept-language": "en-GB,en-US;q=0.8,en;q=0.6",
    "cache-control": "max-age=0",
    "user-agent": "Mozilla/5.0 (page) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/37.0.2062.120 Safari/537.36",
    #'Client-ID':'tq6hq1srip0i37ipzuscegt7viex9fh'   # Just for Twitch API
}


def _get_proxy_url():
    global _PROXY_LIST
    if _PROXY_LIST is None:
        _PROXY_LIST = get_json("http://blissflixx.rocks/feeds/proxies.json")
    p = random.randint(0, len(_PROXY_LIST) - 1)
    return _PROXY_LIST[p]["url"]


def _get_proxy_headers(headers):
    headers = headers.copy()
    headers["origin"] = "blissflixx"
    return headers


def get(url, params=None, proxy=False, session=None, headers=None, timeout=None):
    if not headers:
        headers = _HEADERS
    else:
        headers = headers.copy()
        headers.update(_HEADERS)
    if proxy:
        if params is not None:
            utfparams = {}
            for k, v in params.items():
                utfparams[k] = str(v).encode("utf-8")
            url = url + "?" + urllib.parse.urlencode(utfparams)
        params = {"url": url}
        url = _get_proxy_url()
        headers = _get_proxy_headers(headers)

    if session is None:
        session = new_session()
    r = session.get(url, params=params, headers=headers, timeout=timeout or _TIMEOUT)
    if r.status_code >= 300:
        raise Exception(
            "Request : '"
            + url
            + "' returned: "
            + str(r.status_code)
            + "\nparams:"
            + str(params)
            + "\nheaders:"
            + str(headers)
        )
    return r


def post(url, payload, proxy=False, session=None, timeout=None):
    headers = _HEADERS

    if proxy:
        payload["__url__"] = url
        url = _get_proxy_url()
        headers = _get_proxy_headers(headers)

    if session is None:
        session = new_session()
    r = session.post(url, data=payload, headers=headers, timeout=timeout or _TIMEOUT)
    if r.status_code >= 300:
        raise Exception("Request : '" + url + "' returned: " + str(r.status_code))

    return r


def post_doc(url, payload, **kwargs):
    r = post(url, payload, **kwargs)
    return lxml.html.fromstring(r.text)


def post_json(url, payload, **kwargs):
    r = post(url, payload, **kwargs)
    return r.json()


def get_doc(url, params=None, **kwargs):
    r = get(url, params=params, **kwargs)
    return lxml.html.fromstring(r.text)


def get_json(url, params=None, **kwargs):
    r = get(url, params=params, **kwargs)
    return r.json()


def new_session():
    return requests.session()


def select_one(tree, expr):
    sel = CSSSelector(expr)
    el = sel(tree)
    if isinstance(el, list) and len(el) > 0:
        return el[0]
    else:
        return None


def select_all(tree, expr):
    sel = CSSSelector(expr)
    return sel(tree)


def get_attr(el, name):
    if el is not None:
        return el.get(name)
    else:
        return None


def get_text(el):
    if el is not None and el.text is not None:
        return el.text.strip()
    else:
        return None


def get_text_content(el):
    if el is not None:
        return el.text_content().strip()
    else:
        return None


def byte_size(num, suffix="B"):
    for unit in ["", "K", "M", "G", "T", "P", "E", "Z"]:
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f} Y{suffix}"


def replace_entity(text):
    def fixup(m):
        text = m.group(0)
        if text[:2] == "&#":
            # character reference
            try:
                if text[:3] == "&#x":
                    return chr(int(text[3:-1], 16))
                else:
                    return chr(int(text[2:-1]))
            except ValueError:
                pass
        else:
            # named entity
            try:
                text = chr(html.entities.name2codepoint[text[1:-1]])
            except KeyError:
                pass
            return text  # leave as is

    return re.sub(r"&#?\w+;", fixup, text)


def number_commas(x):
    if type(x) not in [type(0), type(0)]:
        return "0"
    if x < 0:
        return "-" + number_commas(-x)
    result = ""
    while x >= 1000:
        x, r = divmod(x, 1000)
        result = f",{r:03d}{result}"
    return f"{x}{result}"


MOVIE_RE = re.compile(r"(.*)[\(\[]?((?:19|20)\d\d)(?:[^pP\d]|$)[\)\]]?.*$")
SERIES_RE = re.compile(r"(.*)S(\d\d)E(\d\d).*$", re.IGNORECASE)
# Miniseries/documentary parts: "3of3", "Part 2 of 6"
_PART_RE = re.compile(r"\b(?:part\s*)?(\d{1,2})\s*of\s*(\d{1,2})\b", re.IGNORECASE)
# A season marker without an episode: "S02", "Season 2". Used to tell a season
# pack or a full season from a one-off episode or a film. Exclude "S4:"-style
# title prefixes (e.g. "S4: The Bob Lazar Story").
_SEASON_TOKEN_RE = re.compile(r"\bS(\d{1,2})\b(?!\s*E\d{1,2})(?!:)", re.IGNORECASE)
# An episode named by word rather than by SxxEyy: a pack's file is often
# "Show Episode 09.mp4" while the release name belongs to the pack. The
# trailing "\b" keeps "episode 09" from matching the year or a codec; a bare
# "E09" is allowed because that is how a pack names its files too.
_EPISODE_WORD_RE = re.compile(
    r"\b(?:episodes?|ep|e)[\s._-]*0*(\d{1,3})\b", re.IGNORECASE
)
# "Season 2/" or "S02/" as a directory in a path. The trailing slash is what
# tells it from a season marker in a title.
_SEASON_DIR_RE = re.compile(
    r"(?:^|/)\s*(?:season|s)\s*0*(\d{1,2})\s*/", re.IGNORECASE
)
_SEASON_WORD_RE = re.compile(r"\bSeasons?\s*([\d][\d,\s&and\-–to]*)", re.IGNORECASE)
_SEASON_RANGE_RE = re.compile(r"\bS(\d{1,2})\s*[-–to]+\s*S?(\d{1,2})\b", re.IGNORECASE)
_COMPLETE_SERIES_RE = re.compile(
    r"\b(complete\s+series|full\s+series|all\s+seasons|full\s+season|complete\s+seasons?)\b",
    re.IGNORECASE,
)
_COMPLETE_WORD_RE = re.compile(r"\bcomplete\b", re.IGNORECASE)
# Collections that are clearly TV/anime series bundles.
_TV_COLLECTION_RE = re.compile(
    r"\b(ovas?\b.*\bseries\b|\bseries\b.*\bovas?\b|\bspinoffs?\b|\banime\s+series\b)\b",
    re.IGNORECASE,
)
_MOVIE_PACK_RE = re.compile(
    r"\b(collection|trilogy|duology|quadrilogy|pentalogy|saga|anthology|box\s*set|film\s*series|movie\s*pack)\b",
    re.IGNORECASE,
)
# Quality/release tags: where the useful name usually ends.
_CUT_RE = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|bluray|bdrip|brrip|webrip|web[ -]?dl|hdtv|dvdrip|x264|x265|h\.?264|h\.?265|hevc|av1|10bit|remux|proper|repack)\b",
    re.IGNORECASE,
)


def _norm_release_name(name):
    name = name.replace(".", " ").replace("_", " ")
    name = re.sub(r"\s+", " ", name).strip()
    return name


def _clean_title(name):
    """A release name trimmed down to the title part."""
    name = _norm_release_name(name)
    m = _CUT_RE.search(name)
    if m:
        name = name[: m.start()].strip(" -([])")
    # Drop trailing separator junk.
    name = re.sub(r"[\s\-]+$", "", name).strip()
    return name or _norm_release_name(name)


def without_year(name):
    """
    A name without the release's year at the end.

    The pack detector keeps the year inside the series name -- "The Pacific
    2010 Complete Series" reads as the series "The Pacific 2010" -- and that is
    not what OpenSubtitles calls the show. A year that is the whole name
    ("1883") is left alone, because there is nothing left without it.
    """
    trimmed = re.sub(r"\s+[\(\[]?((?:19|20)\d\d)[\)\]]?\s*$", "", name or "")
    return trimmed.strip() or name


def movie_title_year(name):
    name = name.replace(".", " ")
    m = MOVIE_RE.match(name)
    if m is None:
        return {"title": name}
    title = m.group(1)
    if title.endswith("(") or title.endswith("["):
        title = title[:-1]
    title = title.strip()
    year = int(m.group(2))
    return {"title": title, "year": year}


def series_season_episode(name):
    name = name.replace(".", " ")
    m = SERIES_RE.match(name)
    if m is None:
        return {"series": name}
    series = m.group(1).strip()
    season = int(m.group(2))
    episode = int(m.group(3))
    return {"series": series, "season": season, "episode": episode}


def release_kind(name):
    """
    What kind of release this looks like: single episode, TV season/pack,
    movie, or a multi-item pack (collection/complete series).

    Used to pick the right subtitle-search strategy and to recognize container
    torrents. Heuristic by design: it reads the name only.
    """
    norm = _norm_release_name(name)
    ep = SERIES_RE.match(norm)
    if ep:
        return {
            "kind": "episode",
            "series": ep.group(1).strip(),
            "season": int(ep.group(2)),
            "episode": int(ep.group(3)),
        }

    # Miniseries/documentary parts like "3of3".
    part_m = _PART_RE.search(norm)
    if part_m:
        cut = _CUT_RE.search(norm)
        series = norm[: (part_m.start())].strip(" -([])")
        if not series:
            series = norm[: cut.start()].strip(" -([])") if cut else _clean_title(norm)
        return {
            "kind": "episode",
            "series": series or _clean_title(norm),
            "episode": int(part_m.group(1)),
        }

    # Episodes named by word, and sometimes under a season directory:
    # "The Pacific Episode 09.mp4", "Season 2/The Pacific Episode 09.mp4".
    # This is the shape of a file inside a pack, where the release name is the
    # pack's and the file name is all that says which episode it is.
    word_ep = _EPISODE_WORD_RE.search(norm)
    if word_ep:
        prefix = norm[: word_ep.start()]
        season = None
        # The season folder is looked for before the trailing slash is
        # trimmed: "Season 2/" is all a file called "Episode 09.mp4" has.
        folder = _SEASON_DIR_RE.search(prefix)
        if folder:
            season = int(folder.group(1))
            prefix = prefix[folder.end():]
        series = prefix.strip(" -([]/")
        info = {"kind": "episode", "episode": int(word_ep.group(1))}
        # A file may be called just "Episode 09", with the series only in the
        # pack's name. It has no series of its own; the caller fills it in.
        if series:
            info["series"] = series
        if season is not None:
            info["season"] = season
        return info

    # Dated TV episodes (talk shows, sports, dailies): "Show 2026 11 13" / "2026-11-13"
    airdate_m = re.search(r"\b(20\d{2})[\s._-](0?[1-9]|1[0-2])[\s._-](0?[1-9]|[12]\d|3[01])\b", norm)
    if airdate_m:
        series = norm[: airdate_m.start()].strip(" -([])")
        if not series:
            series = _clean_title(norm)
        return {
            "kind": "episode",
            "series": series or _clean_title(norm),
        }

    # TV specials/events commonly labeled as PPV.
    if re.search(r"\bPPV\b", norm):
        cut = _CUT_RE.search(norm)
        series = norm[: cut.start()].strip(" -([])") if cut else _clean_title(norm)
        return {"kind": "episode", "series": series or _clean_title(norm)}

    # TV season markers (without episode): season pack / full season / range.
    seasons = [int(x) for x in _SEASON_TOKEN_RE.findall(norm)]
    seasons = sorted(set(seasons))
    range_m = _SEASON_RANGE_RE.search(norm)
    word_m = _SEASON_WORD_RE.search(norm)
    if range_m:
        a, b = int(range_m.group(1)), int(range_m.group(2))
        lo, hi = min(a, b), max(a, b)
        seasons = sorted(set(seasons) | set(range(lo, hi + 1)))
    elif word_m:
        grp = word_m.group(1) or ""
        # Stop at quality/release tags if they leaked into the captured tail.
        cut = _CUT_RE.search(grp)
        if cut:
            grp = grp[: cut.start()]
        # Use whole number tokens; \d{1,2} would split "1080p" remnants into 10,80.
        nums_all = [int(x) for x in re.findall(r"\d+", grp)]
        resolutions = {360, 480, 576, 720, 1080, 1440, 2160, 4320}
        nums = [n for n in nums_all if 1 <= n <= 99 and n not in resolutions]
        range_word = re.search(r"(\d{1,2})\s*(?:-|–|\bto\b)\s*(\d{1,2})", grp, re.IGNORECASE)
        if range_word:
            lo, hi = int(range_word.group(1)), int(range_word.group(2))
            lo, hi = min(lo, hi), max(lo, hi)
            seasons = sorted(set(seasons) | set(range(lo, hi + 1)))
        elif nums:
            seasons = sorted(set(seasons) | set(nums))

    complete = bool(_COMPLETE_SERIES_RE.search(norm))
    complete_word = bool(_COMPLETE_WORD_RE.search(norm))
    tv_collection = bool(_TV_COLLECTION_RE.search(norm))
    movie_pack_kw = bool(_MOVIE_PACK_RE.search(norm))

    if seasons or complete or tv_collection or (complete_word and not movie_pack_kw):
        # Title: cut before the first season/pack marker or quality tag.
        cut_points = []
        for rx in (_SEASON_RANGE_RE, _SEASON_TOKEN_RE, _SEASON_WORD_RE, _COMPLETE_SERIES_RE, _COMPLETE_WORD_RE, _TV_COLLECTION_RE, _CUT_RE):
            mm = rx.search(norm)
            if mm:
                cut_points.append(mm.start())
        series = norm[: min(cut_points)].strip(" -:") if cut_points else _clean_title(norm)
        # Drop unbalanced trailing parens/brackets left by cutting.
        series = re.sub(r"\s*[\(\[][^)\]]*$", "", series).strip(" -:")
        series = re.sub(r"\s*\((?:19|20)\d{2}$", "", series).strip(" -:")
        series = re.sub(r"\s+\bThe\b$", "", series, flags=re.IGNORECASE).strip(" -:")
        if not series:
            series = _clean_title(norm)
        info = {"kind": "tv_pack", "series": series}
        if len(seasons) == 1:
            info["season"] = seasons[0]
        elif len(seasons) > 1:
            info["seasons"] = seasons
        # A "series pack" spans multiple seasons or is explicitly a complete
        # series. A single season (even if labelled "complete") is a season pack.
        multi_season = len(seasons) > 1
        complete_series = bool(_COMPLETE_SERIES_RE.search(norm)) or tv_collection or (complete_word and not seasons)
        if multi_season or complete_series:
            info["pack"] = True
            info["scope"] = "series"
        else:
            info["scope"] = "season"
        return info

    # Comma-separated list of multiple titles often means a collection/pack.
    comma_parts = [p.strip() for p in re.split(r"\s*,\s*", norm) if p.strip()]
    looks_like_title_list = len(comma_parts) >= 3 and not re.search(r"\bS\d{1,2}E\d{1,2}\b", norm, re.IGNORECASE)
    documentary_collection = bool(re.search(r"\bdocumentaries\b", norm, re.IGNORECASE))

    if movie_pack_kw or looks_like_title_list or documentary_collection:
        title = _clean_title(norm)
        # Try to keep a useful collection title and a leading year if present.
        my = MOVIE_RE.match(norm)
        info = {"kind": "movie_pack", "title": title, "pack": True}
        if movie_pack_kw and my:
            t = my.group(1).strip(" -([])")
            if t:
                info["title"] = t
            info["year"] = int(my.group(2))
        return info

    my = movie_title_year(name)
    if "year" in my:
        return {"kind": "movie", "title": my["title"], "year": my["year"]}
    # Trailing year at end of title (e.g. "Norm ... 2026") not caught by MOVIE_RE.
    trail = re.match(r"^(.*\S)\s+((?:19|20)\d\d)$", norm)
    if trail:
        return {"kind": "movie", "title": trail.group(1).strip(), "year": int(trail.group(2))}
    return {"kind": "unknown", "title": my.get("title", _clean_title(norm))}


def file_name_in_path(path):
    """
    The file name in a torrent path, plus a season folder when it has one.

    A torrent's paths start with the pack's own folder ("The Pacific (2010)
    Mp4 1080p/..."), which is not part of the title. A season folder is,
    because it is the only thing that says which season a file belongs to
    when the file itself is just "Episode 09".
    """
    parts = (path or "").replace("\\", "/").split("/")
    if len(parts) >= 2 and re.match(
        r"^(?:season|s)\s*0*\d{1,2}$", parts[-2], re.IGNORECASE
    ):
        return parts[-2] + "/" + parts[-1]
    return parts[-1]


def title_query(name):
    """
    The title and year a release name is about, for a title-based search.

    A release name is not a title: quality, source, codec and release group are
    in it. An episode or a pack is searched by its series -- an episode has no
    page of its own -- and a film by its title. The year is kept when the name
    has one, because it tells an original from a remake.

        Spider-Man.Brand.New.Day.2026.1080p... -> Spider-Man Brand New Day, 2026
        The.Bear.2022.S01E02...                -> The Bear, 2022
        Scorched...1of3.x264...mkv             -> Scorched Earth Infantry Firepower

    A path also carries the torrent's folder. It supplies the year when the
    row's title is generic ("The Pacific - Complete Mini Series - ..."), and
    the series when the file is called only "Episode 09".
    """
    parts = (name or "").replace("\\", "/").split("/")
    file_part = file_name_in_path(name)
    info = release_kind(file_part)
    kind = info.get("kind")
    if kind in ("episode", "tv_pack"):
        title = info.get("series") or ""
    else:
        title = info.get("title") or info.get("series") or ""
    title = without_year(title).strip()

    folder = parts[0] if len(parts) > 1 else ""
    folder_title = ""
    folder_year = None
    if folder:
        f_info = release_kind(folder)
        f_title = without_year(f_info.get("series") or f_info.get("title") or "")
        # "Season 2" is a folder a file lives in, not the name of a show.
        if not re.match(r"^seasons?\s*\d+$", f_title.strip(), re.IGNORECASE):
            folder_title = f_title.strip()
        folder_year = movie_title_year(folder).get("year")

    if not title and folder_title:
        title = folder_title
    parsed = movie_title_year(file_part)
    if not title:
        title = (parsed.get("title") or "").strip()
    year = parsed.get("year") or folder_year
    if not title:
        # A film whose title is a year ("1917"): the year is the title, and
        # appending it would search for " (1917)".
        title = (_clean_title(file_part) or file_part).strip()
        year = None
    answer = {"title": title}
    if year is not None:
        answer["year"] = year
    return answer


class UrlInfo:
    #: How much of a reply is read. This is asked for a page title, and the same
    #: box takes a stream -- an .m3u8, an .mp4 -- where there is no title at the
    #: top of it. Reading all of a film to look for one is not happening.
    MAX_BYTES = 512 * 1024

    def __init__(self, url):
        self.url = url
        # A streamed read with a timeout, for the same two reasons: the reply
        # may be a film, and a host that never answers must not hold the page.
        with requests.get(url, timeout=_TIMEOUT, stream=True) as r:
            self.page = r.raw.read(self.MAX_BYTES, decode_content=True)
        # An empty reply has no title either; lxml refuses to parse one.
        self.tree = lxml.html.fromstring(self.page if self.page else b"<html></html>")

    def get_html_title(self):
        # The raw <title> text is returned undecoded. This has been changed
        # three times and the current behaviour is the one that works:
        #   d92a9d1  plain text
        #   5fd8537  .encode("latin-1").decode("utf-8)")   <- mojibake hack, buggy
        #   18cc444  html.unescape(text), but then returned the undecoded text
        # html.unescape is still the right call for an "&amp;" in a title, but
        # it was tried and did not fix the reported problem, so returning the
        # decoded form is left as a deliberate decision rather than an
        # accident. Only caller is lib/api/playlink.py, which shows the title
        # in the player.
        #
        # That caller also takes a stream, and a stream has no <title> in it at
        # all: an .m3u8 starts with #EXTM3U. None says so, and playlink names
        # the item from its URL -- this used to index straight through the
        # missing node, which made pasting a stream URL a 500.
        node = self.tree.find(".//title")
        if node is None or not node.text:
            return None
        # decoded = html.unescape(text)
        return node.text

    def get_youtube_video_description(self):
        return self.tree.find(".//meta[@property='og:description']").get("content")

    def get_youtube_video_thumbnail(self):
        return self.tree.find(".//meta[@property='og:image']").get("content")

    def get_youtube_video_publish_date(self):
        return convert_date(
            self.tree.find(".//meta[@itemprop='datePublished']").get("content")
        )

    def get_youtube_video_length(self):
        """
        Returns youtube video length in the format minute(s): seconds from the url
        """
        return convert_duration(
            self.tree.find(".//meta[@itemprop='duration']").get("content")
        )

    def get_youtube_video_channel(self):
        return self.tree.find(".//span[@itemprop='author']/link[@itemprop='name']").get(
            "content"
        )


def convert_date(iso8601):
    dt = datetime.datetime.fromisoformat(iso8601)
    return f"{dt.year}-{dt.month:02d}-{dt.day:02d}"


def convert_duration(iso8601):
    """
    "duration": "PT4M13S",
    The time is formatted as an ISO 8601 string. PT stands for Time Duration, 4M is
    4 minutes, and 13S is 13 seconds.
    For example, "P3Y6M4DT12H30M5S" represents a duration of "three years, six months,
    four days, twelve hours, thirty minutes, and five seconds".
    """
    per_pos = iso8601.find("T")
    per = ""
    if per_pos > 0:
        per = iso8601[1:per_pos]
        iso8601 = iso8601[per_pos + 1 :]
    h_pos = iso8601.find("H")
    h = ""
    if h_pos > 0:
        h = iso8601[:h_pos]
        iso8601 = iso8601[h_pos + 1 :]
    m = ""
    m_pos = iso8601.find("M")
    if m_pos > 0:
        m = iso8601[:m_pos]
        iso8601 = iso8601[m_pos + 1 :]
    if h:
        if len(m) == 0:
            m = "00"
        if len(m) == 1:
            m = "0" + m
    else:
        if not m:
            m = "0"
    s = ""
    s_pos = iso8601.find("S")
    if s_pos > 0:
        s = iso8601[:s_pos]
    if len(s) == 0:
        s = "00"
    elif len(s) == 1:
        s = "0" + s
    if per:
        return f"{per} {h}:{m}:{s}" if h else f"{per} {m}:{s}"
    return f"{h}:{m}:{s}" if h else f"{m}:{s}"

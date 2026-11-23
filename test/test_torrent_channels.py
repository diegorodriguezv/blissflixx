import base64
import codecs

import bfch_divxtotal
import bfch_elitetorrent
import bfch_limetorrents
import bfch_therarbg
import lxml.html

#: One listing with a film, an episode, a music row, and a row that has no
#: torrent link. The last one is what an advertisement looks like in the same
#: table, and it must not become an item.
_LISTING = """
<table class="table2">
  <tr><th class="thleft">Torrent Name</th><th>Added</th></tr>
  <tr>
    <td class="tdleft"><div class="tt-name">
      <a href="http://itorrents.net/torrent/0123456789ABCDEF0123456789ABCDEF01234567.torrent?title=The-Film"
         rel="nofollow" class="csprite_dl14"></a>
      <a href="/The-Film-2020-torrent-1.html">The Film 2020 1080p</a>
    </div></td>
    <td class="tdnormal">1 day ago - in Movies</td>
    <td class="tdnormal">1.50 GB</td>
    <td class="tdseed">1,234</td>
    <td class="tdleech">56</td>
    <td class="tdright"></td>
  </tr>
  <tr>
    <td class="tdleft"><div class="tt-name">
      <a href="http://itorrents.net/torrent/89ABCDEF0123456789ABCDEF0123456789ABCDEF.torrent?title=Some-Show"
         rel="nofollow" class="csprite_dl14"></a>
      <a href="/Some-Show-torrent-2.html">Some Show S02E03 1080p</a>
    </div></td>
    <td class="tdnormal">1 day ago - in TV-shows</td>
    <td class="tdnormal">800 MB</td>
    <td class="tdseed">20</td>
    <td class="tdleech">4</td>
    <td class="tdright"></td>
  </tr>
  <tr>
    <td class="tdleft"><div class="tt-name">
      <a href="http://itorrents.net/torrent/ABCDEF0123456789ABCDEF0123456789ABCDEF01.torrent?title=Album"
         rel="nofollow" class="csprite_dl14"></a>
      <a href="/Album-torrent-3.html">Album 2024 MP3</a>
    </div></td>
    <td class="tdnormal">1 day ago - in Music</td>
    <td class="tdnormal">120 MB</td>
    <td class="tdseed">5</td>
    <td class="tdleech">1</td>
    <td class="tdright"></td>
  </tr>
  <tr>
    <td class="tdleft"><div class="tt-name">
      <a href="/Sponsored-torrent-4.html">Sponsored download</a>
    </div></td>
    <td class="tdnormal">1 day ago</td>
    <td class="tdnormal">1 KB</td>
    <td class="tdseed">9,999</td>
    <td class="tdleech">0</td>
    <td class="tdright"></td>
  </tr>
</table>
"""


def _doc():
    return lxml.html.fromstring(_LISTING)


def test_feedlist_names_the_listings():
    titles = [entry["title"] for entry in bfch_limetorrents.feedlist()]
    assert titles == [
        "Latest Movies",
        "Latest TV Shows",
        "Music",
        "Latest Anime",
    ]


def test_feeds_come_from_the_live_latest_page():
    # Lime's /seeds/1/ page is not a health ranking: its top Movies are 2012
    # releases with huge invented counts and no peers at all. The latest page
    # has live swarms, and _extract sorts those rows by seeds.
    for entry in bfch_limetorrents.feedlist():
        assert "/seeds/" not in entry["url"], entry
        assert entry["url"].endswith("/"), entry


def test_feed_keeps_only_rows_with_a_torrent_link(monkeypatch):
    monkeypatch.setattr(bfch_limetorrents, "get_doc", lambda url: _doc())

    items = bfch_limetorrents.feed(0).to_list()

    assert [item.title for item in items] == [
        "The Film 2020 1080p",
        "Some Show S02E03 1080p",
        "Album 2024 MP3",
    ]


def test_feed_sorts_by_seeds_and_keeps_the_hash(monkeypatch):
    monkeypatch.setattr(bfch_limetorrents, "get_doc", lambda url: _doc())

    item = bfch_limetorrents.feed(0).to_list()[0]

    # The URL is a carrier for the SHA-1. Playback turns the hash into a magnet
    # with our trackers; itorrents.net itself is never fetched.
    assert "0123456789ABCDEF0123456789ABCDEF01234567" in item.url
    assert item.url.endswith("bf_torr_idx=-1")
    assert item.subtitle == "Size: 1.50 GB, Seeds: 1234, Peers: 56"
    assert item.subs["year"] == 2020


def test_feed_reads_the_category_from_search_rows(monkeypatch):
    monkeypatch.setattr(bfch_limetorrents, "get_doc", lambda url: _doc())

    items = bfch_limetorrents.feed(0).to_list()

    episode = items[1]
    assert episode.subs["series"] == "Some Show"
    assert episode.subs["season"] == 2
    assert episode.subs["episode"] == 3

    music = items[2]
    assert music.img == "/img/icons/music.svg"
    assert music.subs is None


def test_anime_without_an_episode_marker_gets_no_subtitle_search(monkeypatch):
    anime = _LISTING.replace(
        "The Film 2020 1080p", "[Group] Some Anime - 02 [1080p]"
    ).replace("in Movies", "in Anime")
    monkeypatch.setattr(
        bfch_limetorrents, "get_doc", lambda url: lxml.html.fromstring(anime)
    )

    item = bfch_limetorrents.feed(3).to_list()[0]

    assert item.subs is None


def test_a_corrupt_seed_count_does_not_top_the_feed(monkeypatch):
    # Lime has a couple of TV rows with millions of seeds on old, small
    # episodes. They are not real swarms and must not lead a seed-ordered
    # feed; the row is skipped rather than ranked first.
    corrupt = _LISTING.replace(
        '<td class="tdseed">1,234</td>',
        '<td class="tdseed">11,204,445</td>',
    )
    monkeypatch.setattr(
        bfch_limetorrents, "get_doc", lambda url: lxml.html.fromstring(corrupt)
    )

    titles = [item.title for item in bfch_limetorrents.feed(0).to_list()]

    assert "The Film 2020 1080p" not in titles
    assert titles[0] == "Some Show S02E03 1080p"


def test_search_quotes_the_query_and_sorts_by_seeds(monkeypatch):
    seen = []

    def fake_get_doc(url):
        seen.append(url)
        return _doc()

    monkeypatch.setattr(bfch_limetorrents, "get_doc", fake_get_doc)

    bfch_limetorrents.search("a b")

    assert seen == ["https://www.limetorrents.fun/search/all/a%20b/seeds/1/"]


_RARBG_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
  <item>
    <title>Film 2020 1080p</title>
    <category>Movies</category>
    <pubDate>Thu, 19 Nov 2026 22:21:51 +0000</pubDate>
    <description>1.5 GB - 50 seeders - quality 47/100</description>
    <enclosure url="magnet:?xt=urn:btih:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA&amp;dn=Film"
               type="application/x-bittorrent"/>
  </item>
  <item>
    <title>Some Show S02E03 1080p</title>
    <category>TV</category>
    <pubDate>Thu, 19 Nov 2026 22:20:00 +0000</pubDate>
    <description>800 MB - 500 seeders - quality 60/100</description>
    <enclosure url="magnet:?xt=urn:btih:BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB&amp;dn=Show"
               type="application/x-bittorrent"/>
  </item>
  <item>
    <title>Album 2024 MP3</title>
    <category>Music</category>
    <pubDate>Thu, 19 Nov 2026 22:19:00 +0000</pubDate>
    <description>120 MB - 10 seeders - quality 40/100</description>
    <enclosure url="magnet:?xt=urn:btih:CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC&amp;dn=Album"
               type="application/x-bittorrent"/>
  </item>
  <item>
    <title>A Book</title>
    <category>Books</category>
    <pubDate>Thu, 19 Nov 2026 22:18:00 +0000</pubDate>
    <description>1 MB - 900 seeders - quality 10/100</description>
    <enclosure url="magnet:?xt=urn:btih:DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD&amp;dn=Book"
               type="application/x-bittorrent"/>
  </item>
  <item>
    <title>No Magnet</title>
    <category>Movies</category>
    <pubDate>Thu, 19 Nov 2026 22:17:00 +0000</pubDate>
    <description>1 MB - 999 seeders - quality 10/100</description>
  </item>
</channel></rss>
"""


class _RssResponse:
    def __init__(self, content):
        self.content = content


def _fake_rarbg(monkeypatch, seen=None):
    def fake_get(url, params=None):
        if seen is not None:
            seen.append((url, params))
        return _RssResponse(_RARBG_RSS.encode("utf-8"))

    monkeypatch.setattr(bfch_therarbg, "get", fake_get)


def test_therarbg_feedlist_names_the_categories():
    assert [entry["title"] for entry in bfch_therarbg.feedlist()] == [
        "Latest Movies",
        "Latest TV Shows",
        "Latest Anime",
        "Latest Music",
    ]


def test_therarbg_feed_asks_for_its_category(monkeypatch):
    seen = []
    _fake_rarbg(monkeypatch, seen)

    items = bfch_therarbg.feed(0).to_list()

    assert seen == [
        ("https://therarbg.com/api/rss/search/", {"q": "", "category": "Movies"})
    ]
    assert [item.title for item in items] == ["Film 2020 1080p"]


def test_therarbg_rows_carry_size_seeds_date_and_an_unescaped_magnet(monkeypatch):
    _fake_rarbg(monkeypatch)

    item = bfch_therarbg.feed(0).to_list()[0]

    assert item.subtitle == "Size: 1.5 GB, Seeds: 50, Released: 2026-11-19"
    assert "&amp;" not in item.url
    assert "&dn=Film" in item.url
    assert item.subs["year"] == 2020


def test_therarbg_search_skips_unplayable_categories_and_sorts_by_seeds(monkeypatch):
    seen = []
    _fake_rarbg(monkeypatch, seen)

    items = bfch_therarbg.search("film").to_list()

    assert seen == [("https://therarbg.com/api/rss/search/", {"q": "film"})]
    assert [item.title for item in items] == [
        "Some Show S02E03 1080p",
        "Film 2020 1080p",
        "Album 2024 MP3",
    ]
    assert items[0].subs["season"] == 2
    assert items[0].subs["episode"] == 3
    assert items[2].img == "/img/icons/music.svg"
    assert items[2].subs is None


_DIVX_MOVIE_B64 = "aHR0cHM6Ly9kaXZ4dG90YWwuZm9vL3dwLWNvbnRlbnQvdXBsb2Fkcy8yMDI2LzExL2EtbW92aWUudG9ycmVudA=="
_DIVX_EP1_B64 = "aHR0cHM6Ly9kaXZ4dG90YWwuZm9vL3dwLWNvbnRlbnQvdXBsb2Fkcy8yMDI2LzExL2Etc2hvdzN4NC50b3JyZW50"
_DIVX_EP2_B64 = "aHR0cHM6Ly9kaXZ4dG90YWwuZm9vL3dwLWNvbnRlbnQvdXBsb2Fkcy8yMDI2LzExL2Etc2hvdzR4MS50b3JyZW50"
_DIVX_FALLBACK_B64 = "aHR0cHM6Ly9kaXZ4dG90YWwuZm9vL3dwLWNvbnRlbnQvdXBsb2Fkcy8yMDI2LzExL2ZhbGxiYWNrLnRvcnJlbnQ="

_DIVX_LIST = """
<table class="table"><thead><tr>
  <th>Nombre</th><th>Categoría</th><th>Fecha</th><th>Tamaño</th>
</tr></thead><tbody>
  <tr><td class="text-left"><a href="https://divxtotal.foo/peliculas/a-movie/" title="A Movie">A Movie</a></td>
      <td><a href="https://divxtotal.foo/category/accion/">Accion</a></td><td>01-01-2026</td><td>1.2 GB</td></tr>
  <tr><td class="text-left"><a href="https://divxtotal.foo/series/a-show/" title="A Show">A Show</a></td>
      <td>Drama</td><td>01-01-2026</td><td>N/A</td></tr>
  <tr><td class="text-left"><a href="https://divxtotal.foo/peliculas/broken/" title="Broken">Broken</a></td>
      <td>Drama</td><td>01-01-2026</td><td>N/A</td></tr>
  <tr><td class="text-left"><a href="https://divxtotal.foo/peliculas/fallback/" title="Fallback">Fallback</a></td>
      <td>Drama</td><td>01-01-2026</td><td>N/A</td></tr>
</tbody></table>
"""

_DIVX_MOVIE = (
    """
<html><body>
<h1>A Movie</h1>
<div id="bloque_poster"><img src="https://divxtotal.foo/posters/a-movie.jpg"></div>
<div class="info-item"><p class="tagitem">Idioma:&nbsp;</p><p>Español</p></div>
<div class="info-item"><p class="tagitem">Formato:&nbsp;</p><p>MKV</p></div>
<table class="rwd-table">
  <tr><th>Idioma</th><th>Formato</th><th>Calidad</th><th>Peso</th><th>Descargar</th></tr>
  <tr><td><img src="https://divxtotal.foo/wp-content/themes/DivxTotal/css/images/ES.png"></td>
      <td>MKV</td><td>1080p</td><td>2.1 GB</td>
      <td><a class="linktorrent" data-src="%s" href="https://short-info.link/a">Descargar</a></td></tr>
</table>
</body></html>
"""
    % _DIVX_MOVIE_B64
)

_DIVX_SERIES = """
<html><body>
<h1>A Show</h1>
<div id="bloque_poster"><img src="https://divxtotal.foo/posters/a-show.jpg"></div>
<table class="rwd-table">
  <tr><th>Capítulo</th><th>Opcion 1</th><th>Opcion 2</th></tr>
  <tr><td><img data-pagespeed-lazy-src="https://divxtotal.foo/css/images/ES.png" src="/p.gif">
      <a class="linktorrent" data-src="%s" href="https://short-info.link/b">A Show3x4</a></td>
      <td><a class="linktorrent" data-src="%s" href="https://short-info.link/c">dup</a></td></tr>
  <tr><td><img data-pagespeed-lazy-src="https://divxtotal.foo/css/images/ES.png" src="/p.gif">
      <a class="linktorrent" data-src="%s" href="https://short-info.link/d">A Show4x1</a></td>
      <td></td></tr>
</table>
</body></html>
""" % (
    _DIVX_EP1_B64,
    _DIVX_EP1_B64,
    _DIVX_EP2_B64,
)

_DIVX_FALLBACK = (
    """
<html><body><h1>Fallback Film</h1>
<table class="rwd-table">
  <tr><td><img src="https://divxtotal.foo/css/images/ES.png"></td>
      <td>AVI</td><td>DVDRip</td><td>700 MB</td>
      <td><a class="linktorrent" href="https://divxtotal.foo/download_tt.php?u=%s">Descargar</a></td></tr>
</table>
</body></html>
"""
    % _DIVX_FALLBACK_B64
)


def _fake_divx(monkeypatch, seen=None):
    def fake_doc(url):
        if seen is not None:
            seen.append(url)
        if "a-movie" in url:
            return lxml.html.fromstring(_DIVX_MOVIE)
        if "a-show" in url:
            return lxml.html.fromstring(_DIVX_SERIES)
        if "fallback" in url:
            return lxml.html.fromstring(_DIVX_FALLBACK)
        if "broken" in url:
            raise RuntimeError("gone")
        return lxml.html.fromstring(_DIVX_LIST)

    monkeypatch.setattr(bfch_divxtotal, "get_doc", fake_doc)


def test_divx_feedlist_names_the_categories():
    assert [entry["title"] for entry in bfch_divxtotal.feedlist()] == [
        "Películas",
        "Películas 4K",
        "Películas DVDR",
    ]


def test_divx_detail_pages_become_items_and_a_broken_one_is_skipped(monkeypatch):
    _fake_divx(monkeypatch)

    titles = [item.title for item in bfch_divxtotal.feed(0).to_list()]

    assert titles == [
        "A Movie [1080p]",
        "A Show S03E04",
        "Fallback Film [DVDRip]",
        "A Show S04E01",
    ]


def test_divx_movie_row_decodes_the_torrent_and_reads_the_info(monkeypatch):
    _fake_divx(monkeypatch)

    item = bfch_divxtotal.feed(0).to_list()[0]

    assert item.url.startswith("https://divxtotal.foo/")
    assert "a-movie.torrent" in item.url
    assert item.img == "https://divxtotal.foo/posters/a-movie.jpg"
    assert (
        item.subtitle == "Idioma: Español, Calidad: 1080p, Formato: MKV, Peso: 2.1 GB"
    )
    assert item.subs["title"] == "A Movie"


def test_divx_series_rows_become_episodes_with_season_and_episode(monkeypatch):
    _fake_divx(monkeypatch)

    items = bfch_divxtotal.feed(0).to_list()
    first = items[1]

    assert first.title == "A Show S03E04"
    assert first.subs["series"] == "A Show"
    assert first.subs["season"] == 3
    assert first.subs["episode"] == 4


def test_divx_download_tt_fallback_is_decoded(monkeypatch):
    _fake_divx(monkeypatch)

    item = next(
        item
        for item in bfch_divxtotal.feed(0).to_list()
        if item.title == "Fallback Film [DVDRip]"
    )

    assert "fallback.torrent" in item.url
    assert (
        item.subtitle == "Idioma: Español, Calidad: DVDRip, Formato: AVI, Peso: 700 MB"
    )


def test_divx_search_uses_the_site_query(monkeypatch):
    seen = []
    _fake_divx(monkeypatch, seen)

    bfch_divxtotal.search("audio latino")

    assert seen[0] == "https://divxtotal.foo/?s=audio%20latino"


def test_divx_non_ascii_torrent_url_is_percent_encoded():
    data = base64.b64encode(
        "https://divxtotal.foo/Ted-Lasso-4×03.mp4.torrent".encode("utf-8")
    ).decode("ascii")
    link = lxml.html.fromstring('<a class="linktorrent" data-src="%s"></a>' % data)

    url = bfch_divxtotal._torrent_url(link)

    assert url == "https://divxtotal.foo/Ted-Lasso-4%C3%9703.mp4.torrent"


_ELITE_TORRENT = (
    "https://www.elitetorrent.com/wp-content/uploads/2026/11/carrera.torrent"
)
_ELITE_MAGNET = "magnet:?xt=urn:btih:" + "E" * 40 + "&dn=Carrera"


def _elite_href(url):
    value = codecs.encode(url, "rot_13")
    for _ in range(5):
        value = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return "https://acortame-esto.com/s.php?i=%s&st=et" % value


_ELITE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
  <item>
    <title>Carrera de bestias</title>
    <link>https://www.elitetorrent.com/peliculas/carrera-de-bestias/</link>
    <pubDate>Mon, 16 Nov 2026 02:50:08 +0000</pubDate>
    <media:content url="https://www.elitetorrent.com/carrera.jpg" medium="image"/>
  </item>
  <item>
    <title>Peaky Blinders 6×5</title>
    <link>https://www.elitetorrent.com/series/peaky-blinders-65/</link>
    <pubDate>Sun, 15 Nov 2026 10:00:00 +0000</pubDate>
    <media:content url="https://www.elitetorrent.com/peaky.jpg" medium="image"/>
  </item>
  <item>
    <title>Loki T01E05</title>
    <link>https://www.elitetorrent.com/series/loki-t01e05/</link>
    <pubDate>Sat, 14 Nov 2026 10:00:00 +0000</pubDate>
    <media:content url="https://www.elitetorrent.com/loki.jpg" medium="image"/>
  </item>
</channel></rss>
"""

_ELITE_MOVIE = """
<html><body>
<a class="enlace_torrent" href="%s">Descargar el .torrent</a>
<a class="enlace_torrent" href="%s">Descargar por magnet link</a>
</body></html>
""" % (
    _elite_href(_ELITE_TORRENT),
    _elite_href(_ELITE_MAGNET),
)

_ELITE_SERIES = """
<html><body>
<a class="enlace_torrent" href="%s">Descargar por magnet link</a>
</body></html>
""" % _elite_href(
    _ELITE_MAGNET
)

_ELITE_TORRENT_ONLY = """
<html><body>
<a class="enlace_torrent" href="%s">Descargar el .torrent</a>
</body></html>
""" % _elite_href(
    _ELITE_TORRENT
)


class _EliteResponse:
    def __init__(self, content):
        self.content = content


def _fake_elite(monkeypatch, seen=None, detail=None):
    def fake_get(url, params=None):
        if seen is not None:
            seen.append((url, params))
        return _EliteResponse(_ELITE_RSS.encode("utf-8"))

    monkeypatch.setattr(bfch_elitetorrent, "get", fake_get)

    def fake_doc(url):
        if detail is not None:
            return lxml.html.fromstring(detail)
        if "peaky" in url or "loki" in url:
            return lxml.html.fromstring(_ELITE_SERIES)
        return lxml.html.fromstring(_ELITE_MOVIE)

    monkeypatch.setattr(bfch_elitetorrent, "get_doc", fake_doc)


def test_elitetorrent_feedlist_names_the_sections():
    assert [entry["title"] for entry in bfch_elitetorrent.feedlist()] == [
        "Películas",
        "Series",
        "1080p",
        "4K",
    ]


def test_elitetorrent_feed_reads_the_rss_and_prefers_the_magnet(monkeypatch):
    _fake_elite(monkeypatch)

    items = bfch_elitetorrent.feed(0).to_list()

    assert [item.title for item in items] == [
        "Carrera de bestias",
        "Peaky Blinders 6×5",
        "Loki T01E05",
    ]
    assert items[0].img == "https://www.elitetorrent.com/carrera.jpg"
    assert items[0].subtitle == "Released: 2026-11-16"
    assert items[0].url.startswith("magnet:")
    assert items[0].subs["title"] == "Carrera de bestias"


def test_elitetorrent_series_titles_get_season_and_episode(monkeypatch):
    _fake_elite(monkeypatch)

    items = bfch_elitetorrent.feed(1).to_list()

    assert items[1].subs["series"] == "Peaky Blinders"
    assert items[1].subs["season"] == 6
    assert items[1].subs["episode"] == 5
    assert items[2].subs["series"] == "Loki"
    assert items[2].subs["season"] == 1
    assert items[2].subs["episode"] == 5


def test_elitetorrent_1080p_feed_labels_the_quality(monkeypatch):
    _fake_elite(monkeypatch)

    item = bfch_elitetorrent.feed(2).to_list()[0]

    assert item.subtitle == "Calidad: 1080p, Released: 2026-11-16"


def test_elitetorrent_search_passes_the_query_to_the_rss_feed(monkeypatch):
    seen = []
    _fake_elite(monkeypatch, seen)

    bfch_elitetorrent.search("peaky")

    assert seen[0] == (
        "https://www.elitetorrent.com/",
        {"s": "peaky", "feed": "rss2"},
    )


def test_elitetorrent_falls_back_to_the_torrent_link(monkeypatch):
    _fake_elite(monkeypatch, detail=_ELITE_TORRENT_ONLY)

    item = bfch_elitetorrent.feed(0).to_list()[0]

    assert item.url.startswith("https://www.elitetorrent.com/")
    assert "carrera.torrent" in item.url

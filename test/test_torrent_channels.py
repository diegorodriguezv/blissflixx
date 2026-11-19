import bfch_limetorrents
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
        "Popular Movies",
        "Latest TV Shows",
        "Popular TV Shows",
        "Latest Music",
        "Latest Anime",
    ]


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

    item = bfch_limetorrents.feed(5).to_list()[0]

    assert item.subs is None


def test_search_quotes_the_query_and_sorts_by_seeds(monkeypatch):
    seen = []

    def fake_get_doc(url):
        seen.append(url)
        return _doc()

    monkeypatch.setattr(bfch_limetorrents, "get_doc", fake_get_doc)

    bfch_limetorrents.search("a b")

    assert seen == ["https://www.limetorrents.fun/search/all/a%20b/seeds/1/"]

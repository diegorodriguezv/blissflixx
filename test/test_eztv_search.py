"""
EZTV's browse feeds, its trending list and its show-first search.

EZTV's API answers by IMDb id and offers no title search, sorting, genres or
countries; its HTML pages are behind Cloudflare. The channel therefore uses
TVMaze's browse page for genres, types and countries and TVMaze's search for a
title, while Trending is built from EZTV's own recent seeds. The fixtures are
recorded shapes; no test reaches the network.
"""

import blissflixx.channels.bfch_eztv as eztv
import lxml.html
import pytest

from blissflixx.api.common import ApiError


def card(tvmaze_id, name, rating="7.9", votes="367"):
    return (
        '<div data-key="%s">'
        '<figure class="image"><a href="/shows/%s/x">'
        '<img src="https://img/%s.jpg"></a></figure>'
        '<div class="content"><span class="title"><h2>'
        '<a href="/shows/%s/x">%s</a></h2></span>'
        "<div>%s (%s votes)</div></div></div>"
        % (tvmaze_id, tvmaze_id, tvmaze_id, tvmaze_id, name, rating, votes)
    )


PAGE = "<html><body>%s%s</body></html>" % (
    card(1, "Show One"),
    card(2, "Show Two", rating="8.1", votes="1200"),
)


def torrent(
    title,
    imdb="1190634",
    season="5",
    episode="8",
    seeds=10,
    magnet="magnet:?xt=urn:btih:" + "A" * 40,
):
    return {
        "title": title,
        "imdb_id": imdb,
        "season": season,
        "episode": episode,
        "seeds": seeds,
        "magnet_url": magnet,
        "size_bytes": "734003200",
        "date_released_unix": "1791957953",
    }


def tv_show(
    name,
    imdb="tt1190634",
    premiered="2024-11-14",
    network="Food Network",
    genres=None,
):
    return {
        "show": {
            "name": name,
            "premiered": premiered,
            "network": {"name": network},
            "webChannel": None,
            "genres": genres or ["Food"],
            "rating": {"average": 8.0},
            "image": {"medium": "https://img/%s.jpg" % name},
            "externals": {"imdb": imdb},
        }
    }


def _no_tvmaze(url, **kwargs):
    """TVMaze cannot be reached from a test; the rows fall back to the old look."""
    raise RuntimeError("no TVMaze in this test")


@pytest.fixture(autouse=True)
def clear_cache():
    eztv._CACHE.clear()
    yield
    eztv._CACHE.clear()


class TestTheFeeds:
    def test_every_feed_has_a_unique_name(self):
        names = [feed.get("name") for feed in eztv.feedlist()]
        assert all(names)
        assert len(set(names)) == len(names)

    def test_a_name_resolves_after_the_list_moves(self, monkeypatch):
        monkeypatch.setattr(eztv, "_latest_page", lambda page: {"torrents": []})
        assert eztv._feed_index(0, "trending") == 1
        # The same name still finds Trending from an index that now points
        # somewhere else; an unknown name falls back to that index.
        assert eztv._feed_index(5, "trending") == 1
        assert eztv._feed_index(5, "no-such-feed") == 5

    def test_latest_is_eztrs_own_feed(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "_latest_page",
            lambda page: {"torrents": [torrent("A Show S01E01")]},
        )
        assert [row.title for row in eztv.feed(0).to_list()] == ["A Show S01E01"]

    def test_latest_is_sorted_by_seeds(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "_latest_page",
            lambda page: {
                "torrents": [
                    torrent("Low S01E01", seeds=5),
                    torrent("High S01E02", seeds=50),
                    torrent("Mid S01E03", seeds=20),
                ]
            },
        )
        assert [row.title for row in eztv.feed(0).to_list()] == [
            "High S01E02",
            "Mid S01E03",
            "Low S01E01",
        ]

    def test_a_show_page_is_sorted_by_seeds(self, monkeypatch):
        # The show page used to sort by season and episode. Seeds are what the
        # row is chosen by, so an older but busier episode comes first.
        monkeypatch.setattr(
            eztv,
            "_episodes",
            lambda imdb: [
                torrent("Old S01E01", season="1", episode="1", seeds=5),
                torrent("New S01E02", season="1", episode="2", seeds=50),
            ],
        )
        assert [row.title for row in eztv._episode_rows("123", "Show").to_list()] == [
            "New S01E02",
            "Old S01E01",
        ]

    def test_browse_feeds_use_their_filter(self, monkeypatch):
        calls = []

        def fake_doc(url, **kwargs):
            calls.append(kwargs.get("params"))
            return lxml.html.fromstring(PAGE)

        monkeypatch.setattr(eztv, "get_doc", fake_doc)
        rows = eztv.feed_by_name(0, "genre-comedy").to_list()
        assert calls[0] == {"Show[genre]": "2"}
        assert [row.title for row in rows] == ["Show One", "Show Two"]


class TestTheBrowseRows:
    def test_a_browse_feed_is_show_rows(self, monkeypatch):
        monkeypatch.setattr(
            eztv, "get_doc", lambda url, **kwargs: lxml.html.fromstring(PAGE)
        )
        rows = eztv._tvmaze_shows({"Show[genre]": "1"}).to_list()
        assert [row.title for row in rows] == ["Show One", "Show Two"]
        assert rows[0].url == "showmore://tvmaze:1"
        assert rows[0].img == "https://img/1.jpg"
        assert "7.9" in rows[0].subtitle

    def test_the_browse_page_is_read_once(self, monkeypatch):
        calls = []

        def fake_doc(url, **kwargs):
            calls.append(url)
            return lxml.html.fromstring(PAGE)

        monkeypatch.setattr(eztv, "get_doc", fake_doc)
        eztv._tvmaze_shows({"Show[genre]": "1"})
        eztv._tvmaze_shows({"Show[genre]": "1"})
        assert len(calls) == 1

    def test_a_browse_failure_is_a_service_error(self, monkeypatch):
        def boom(url, **kwargs):
            raise RuntimeError("no route to host")

        monkeypatch.setattr(eztv, "get_doc", boom)
        with pytest.raises(ApiError) as caught:
            eztv._tvmaze_shows({"Show[genre]": "1"})
        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE


class TestLatest:
    """The new releases, carrying each show's poster like the other pages."""

    def test_rows_carry_their_shows_posters(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "_latest_page",
            lambda page: {
                "torrents": [
                    torrent("Show A S01E01", imdb="1"),
                    torrent("Show A S01E01 720p", imdb="1"),
                    torrent("Show B S01E01", imdb="2"),
                ]
            },
        )

        def fake(url, **kwargs):
            if kwargs["params"]["imdb"] == "tt1":
                return {
                    "name": "Show A",
                    "image": {"medium": "https://img/a.jpg"},
                }
            raise RuntimeError("not found")

        monkeypatch.setattr(eztv, "get_json", fake)
        rows = eztv.feed(0).to_list()
        # One lookup serves both of the show's releases.
        assert rows[0].img == "https://img/a.jpg"
        assert rows[1].img == "https://img/a.jpg"
        # A show TVMaze does not know keeps the icon.
        assert rows[2].img == "/img/icons/tv.svg"

    def test_a_build_looks_up_only_so_many_new_shows(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "_latest_page",
            lambda page: {
                "torrents": [
                    torrent("Show S01E0%d" % n, imdb=str(n)) for n in range(1, 7)
                ]
            },
        )
        monkeypatch.setattr(eztv, "_LATEST_FRESH", 3)
        calls = []

        def fake(url, **kwargs):
            calls.append(kwargs["params"]["imdb"])
            return {"name": "A show", "image": {"medium": "https://img/x.jpg"}}

        monkeypatch.setattr(eztv, "get_json", fake)
        eztv.feed(0)
        assert len(calls) == 3
        # The next build finds those in the cache and finishes the page.
        eztv.feed(0)
        assert len(calls) == 6


class TestTrending:
    def _pages(self, monkeypatch, pages):
        monkeypatch.setattr(
            eztv,
            "_latest_page",
            lambda page: pages.get(page, {"torrents": []}),
        )

    def test_it_sums_the_seeds_of_a_show(self, monkeypatch):
        self._pages(
            monkeypatch,
            {
                1: {
                    "torrents": [
                        torrent(
                            "Low Show S01E01",
                            imdb="2",
                            season="1",
                            episode="1",
                            seeds=5,
                        ),
                        torrent(
                            "High Show S01E02",
                            imdb="1",
                            season="1",
                            episode="2",
                            seeds=50,
                        ),
                    ]
                },
                2: {
                    "torrents": [
                        torrent(
                            "High Show S01E03",
                            imdb="1",
                            season="1",
                            episode="3",
                            seeds=40,
                        )
                    ]
                },
            },
        )
        # TVMaze is asked for each show's poster; when it cannot answer, the
        # row keeps the release name and an icon, which is the old look.
        monkeypatch.setattr(eztv, "get_json", _no_tvmaze)
        rows = eztv._trending_shows().to_list()
        assert [row.title for row in rows] == ["High Show", "Low Show"]
        assert rows[0].url == "showmore://imdb:1|High Show"
        assert "2 releases" in rows[0].subtitle
        assert "90 seeds" in rows[0].subtitle
        assert "S01E03" in rows[0].subtitle

    def test_it_skips_rows_without_a_magnet_or_an_id(self, monkeypatch):
        self._pages(
            monkeypatch,
            {
                1: {
                    "torrents": [
                        torrent("No Id Show S01E01", imdb=""),
                        torrent("No Magnet S01E01", magnet=None),
                        torrent("Kept S01E01", imdb="9"),
                    ]
                }
            },
        )
        monkeypatch.setattr(eztv, "get_json", _no_tvmaze)
        rows = eztv._trending_shows().to_list()
        assert [row.title for row in rows] == ["Kept"]

    def test_rows_carry_the_shows_poster_and_own_name(self, monkeypatch):
        self._pages(
            monkeypatch,
            {
                1: {
                    "torrents": [
                        torrent("Georgie and Mandys S01E01", imdb="1", seeds=90)
                    ]
                }
            },
        )
        show = {
            "name": "Georgie & Mandy's First Marriage",
            "premiered": "2024-10-17",
            "network": {"name": "CBS"},
            "webChannel": None,
            "genres": ["Comedy"],
            "rating": {"average": 8.3},
            "image": {"medium": "https://img/poster.jpg"},
        }
        monkeypatch.setattr(eztv, "get_json", lambda url, **kwargs: show)
        row = eztv._trending_shows().to_list()[0]
        assert row.title == "Georgie & Mandy's First Marriage"
        assert row.img == "https://img/poster.jpg"
        assert row.subtitle == "2024 · CBS · Comedy · 8.3"
        assert "90 seeds" in row.synopsis
        assert row.url == "showmore://imdb:1|Georgie & Mandy's First Marriage"

    def test_the_show_lookup_is_kept_for_a_day(self, monkeypatch):
        self._pages(
            monkeypatch,
            {1: {"torrents": [torrent("High Show S01E02", imdb="1", seeds=50)]}},
        )
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return {
                "name": "High Show",
                "premiered": "2024-01-01",
                "network": {"name": "HBO"},
                "genres": [],
                "image": {"medium": "https://img/x.jpg"},
            }

        monkeypatch.setattr(eztv, "get_json", fake)
        eztv._trending_shows()
        eztv._trending_shows()
        assert len(calls) == 1

    def test_a_first_page_failure_is_a_service_error(self, monkeypatch):
        def boom(page):
            raise RuntimeError("timeout")

        monkeypatch.setattr(eztv, "_latest_page", boom)
        with pytest.raises(ApiError) as caught:
            eztv._trending_shows()
        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE

    def test_the_list_is_kept_for_a_while(self, monkeypatch):
        calls = []

        def page(number):
            calls.append(number)
            return {"torrents": []}

        monkeypatch.setattr(eztv, "_latest_page", page)
        eztv._trending_shows()
        eztv._trending_shows()
        assert len(calls) == eztv._TRENDING_PAGES


class TestSearch:
    def test_one_match_opens_its_episodes(self, monkeypatch):
        monkeypatch.setattr(
            eztv, "get_json", lambda url, **kwargs: [tv_show("The Boys", "tt1190634")]
        )
        monkeypatch.setattr(
            eztv, "_episodes", lambda imdb: [torrent("The Boys S01E01")]
        )
        rows = eztv.search("the boys").to_list()
        assert [row.title for row in rows] == ["The Boys S01E01"]
        # The page the search opens carries the poster it already had.
        assert rows[0].img == "https://img/The Boys.jpg"

    def test_several_matches_are_a_picker(self, monkeypatch):
        matches = [
            tv_show("Harry Potter", "tt13918446", "2026-12-25", "HBO"),
            tv_show("Harry Potter: Wizards of Baking", "tt32374361"),
        ]
        monkeypatch.setattr(eztv, "get_json", lambda url, **kwargs: matches)

        def no_episodes(imdb):
            raise AssertionError("a picker must not ask EZTV yet")

        monkeypatch.setattr(eztv, "_episodes", no_episodes)
        rows = eztv.search("harry potter").to_list()
        assert [row.title for row in rows] == [
            "Harry Potter",
            "Harry Potter: Wizards of Baking",
        ]
        assert rows[0].url == "showmore://imdb:13918446|Harry Potter"
        assert "2026" in rows[0].subtitle
        assert "HBO" in rows[0].subtitle
        assert rows[1].url == "showmore://imdb:32374361|Harry Potter: Wizards of Baking"

    def test_a_pasted_id_skips_the_search(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return {"name": "The Boys", "image": {"medium": "https://img/x.jpg"}}

        monkeypatch.setattr(eztv, "get_json", fake)
        monkeypatch.setattr(
            eztv, "_episodes", lambda imdb: [torrent("An episode", imdb=imdb)]
        )
        rows = eztv.search("tt1190634").to_list()
        # No TVMaze search: the id is the whole query. The poster is still
        # looked up, as on every show page.
        assert calls == [eztv._TVMAZE_LOOKUP]
        assert rows[0].subs["imdb"] == "tt1190634"

    def test_no_match_is_an_error(self, monkeypatch):
        monkeypatch.setattr(eztv, "get_json", lambda url, **kwargs: [])
        with pytest.raises(ApiError) as caught:
            eztv.search("qwertyuiopasdfgh")
        assert caught.value.status == 404
        assert "No show found" in str(caught.value)
        assert "IMDb id" in str(caught.value)

    def test_shows_without_an_imdb_id_are_not_offered(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "get_json",
            lambda url, **kwargs: [tv_show("Icons Unearthed", imdb=None)],
        )
        with pytest.raises(ApiError) as caught:
            eztv.search("icons unearthed")
        assert caught.value.status == 404

    def test_a_lookup_failure_is_a_service_error(self, monkeypatch):
        def boom(url, **kwargs):
            raise RuntimeError("no route to host")

        monkeypatch.setattr(eztv, "get_json", boom)
        with pytest.raises(ApiError) as caught:
            eztv.search("the boys")
        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE


class TestShowmore:
    def test_a_tvmaze_show_resolves_its_imdb(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append((url, kwargs.get("params") or {}))
            if url.startswith("https://api.tvmaze.com/shows/"):
                return {
                    "name": "The Boys",
                    "externals": {"imdb": "tt1190634"},
                    "image": {"medium": "https://img/boys.jpg"},
                }
            return {"torrents_count": 0, "torrents": []}

        monkeypatch.setattr(eztv, "get_json", fake)
        monkeypatch.setattr(
            eztv, "_episodes", lambda imdb: [torrent("The Boys S05E08")]
        )
        rows = eztv.showmore("tvmaze:1").to_list()
        assert calls[0][0].endswith("/shows/1")
        assert rows[0].subs["imdb"] == "tt1190634"
        assert rows[0].img == "https://img/boys.jpg"

    def test_an_imdb_link_uses_the_cached_poster(self, monkeypatch):
        # A trending or picker row has already looked the show up; the page
        # reuses that and costs no request.
        eztv._cache(
            ("show", "1190634"),
            {"name": "The Boys", "image": {"medium": "https://img/boys.jpg"}},
        )
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return {"torrents_count": 0, "torrents": []}

        monkeypatch.setattr(eztv, "get_json", fake)
        monkeypatch.setattr(eztv, "_episodes", lambda imdb: [torrent("An episode")])
        rows = eztv.showmore("imdb:1190634|The Boys").to_list()
        assert calls == []
        assert rows[0].img == "https://img/boys.jpg"

    def test_an_imdb_link_without_a_cache_looks_the_show_up(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return {
                "name": "The Boys",
                "image": {"medium": "https://img/boys.jpg"},
            }

        monkeypatch.setattr(eztv, "get_json", fake)
        monkeypatch.setattr(eztv, "_episodes", lambda imdb: [torrent("An episode")])
        rows = eztv.showmore("imdb:1190634|The Boys").to_list()
        assert calls == [eztv._TVMAZE_LOOKUP]
        assert rows[0].img == "https://img/boys.jpg"

    def test_a_show_without_an_imdb_id_says_so(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "get_json",
            lambda url, **kwargs: {"name": "No Id", "externals": {"imdb": None}},
        )
        with pytest.raises(ApiError) as caught:
            eztv.showmore("tvmaze:1")
        assert caught.value.status == 404
        assert "No Id" in str(caught.value)

    def test_a_show_without_episodes_says_so(self, monkeypatch):
        monkeypatch.setattr(
            eztv,
            "get_json",
            lambda url, **kwargs: {"name": "Empty", "externals": {"imdb": "tt123"}},
        )
        monkeypatch.setattr(eztv, "_episodes", lambda imdb: [])
        with pytest.raises(ApiError) as caught:
            eztv.showmore("tvmaze:1")
        assert caught.value.status == 404
        assert "Empty" in str(caught.value)

    def test_an_unknown_link_is_refused(self):
        with pytest.raises(ApiError) as caught:
            eztv.showmore("something:else")
        assert caught.value.status == 400


class TestTheEpisodes:
    def test_the_newest_episode_comes_first(self, monkeypatch):
        torrents = [
            torrent("S05E07", season="5", episode="7"),
            torrent("S05E08", season="5", episode="8"),
            torrent("S04E10", season="4", episode="10"),
        ]
        monkeypatch.setattr(eztv, "_episodes", lambda imdb: list(torrents))
        titles = [row.title for row in eztv._episode_rows("1", "Show").to_list()]
        assert titles == ["S05E08", "S05E07", "S04E10"]

    def test_releases_of_one_episode_are_ordered_by_seeds(self, monkeypatch):
        torrents = [
            torrent("S05E08 720p", season="5", episode="8", seeds=5),
            torrent("S05E08 1080p", season="5", episode="8", seeds=50),
        ]
        monkeypatch.setattr(eztv, "_episodes", lambda imdb: list(torrents))
        titles = [row.title for row in eztv._episode_rows("1", "Show").to_list()]
        assert titles == ["S05E08 1080p", "S05E08 720p"]

    def test_the_rows_carry_the_imdb_id_for_subtitles(self, monkeypatch):
        monkeypatch.setattr(eztv, "_episodes", lambda imdb: [torrent("An episode")])
        row = eztv._episode_rows("1190634", "Show").to_list()[0]
        assert row.subs["imdb"] == "tt1190634"

    def test_it_reads_the_pages_the_show_needs(self, monkeypatch):
        pages = {
            1: {"torrents_count": 250, "torrents": [torrent("S05E01")]},
            2: {
                "torrents_count": 250,
                "torrents": [torrent("S03E01", season="3", episode="1")],
            },
            3: {
                "torrents_count": 250,
                "torrents": [torrent("S01E01", season="1", episode="1")],
            },
        }
        calls = []

        def fake(url, **kwargs):
            page = kwargs["params"]["page"]
            calls.append(page)
            return pages[page]

        monkeypatch.setattr(eztv, "get_json", fake)
        rows = eztv._episodes("1190634")
        assert calls == [1, 2, 3]
        assert len(rows) == 3

    def test_it_stops_at_the_page_cap(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            page = kwargs["params"]["page"]
            calls.append(page)
            return {"torrents_count": 100000, "torrents": [torrent("S05E01")]}

        monkeypatch.setattr(eztv, "get_json", fake)
        eztv._episodes("1190634")
        assert calls == [1, 2, 3, 4, 5]

"""
EZTV's title search.

EZTV's API answers by IMDb id, not by title, and its HTML search is behind
Cloudflare. A title is therefore resolved with TVMaze first, and the show's id
is what EZTV is asked for. The fixtures are recorded shapes, not live calls.
"""

import pytest

import bfch_eztv as eztv
from lib.api.common import ApiError


TVMAZE = [
    {
        "score": 0.9,
        "show": {"name": "The Boys", "externals": {"imdb": "tt1190634"}},
    }
]


def torrent(
    title,
    season="5",
    episode="8",
    seeds=10,
    magnet="magnet:?xt=urn:btih:" + "A" * 40,
):
    return {
        "title": title,
        "season": season,
        "episode": episode,
        "seeds": seeds,
        "magnet_url": magnet,
        "size_bytes": "734003200",
        "date_released_unix": "1791957953",
    }


class TestResolvingTheShow:
    def test_a_title_is_looked_up_on_tvmaze(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append((url, kwargs.get("params") or {}))
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            return {"torrents_count": 1, "torrents": [torrent("The Boys S05E08")]}

        monkeypatch.setattr(eztv, "get_json", fake)
        rows = eztv.search("the boys").to_list()
        assert rows[0].title == "The Boys S05E08"
        assert calls[0][0] == eztv._TVMAZE_SEARCH
        assert calls[0][1]["q"] == "the boys"
        assert calls[1][1]["imdb_id"] == "1190634"

    def test_a_pasted_id_skips_the_lookup(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append(url)
            return {"torrents_count": 1, "torrents": [torrent("An episode")]}

        monkeypatch.setattr(eztv, "get_json", fake)
        eztv.search("tt1190634")
        assert eztv._TVMAZE_SEARCH not in calls
        assert calls == [eztv._BASE_API]

    def test_digits_without_tt_are_an_id_too(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            calls.append((url, kwargs.get("params") or {}))
            return {"torrents_count": 1, "torrents": [torrent("An episode")]}

        monkeypatch.setattr(eztv, "get_json", fake)
        eztv.search("1190634")
        assert calls[0][1]["imdb_id"] == "1190634"

    def test_a_show_that_is_not_found_says_so(self, monkeypatch):
        monkeypatch.setattr(eztv, "get_json", lambda url, **kwargs: [])
        with pytest.raises(ApiError) as caught:
            eztv.search("a show that does not exist")
        assert caught.value.status == 404
        assert "No show found" in str(caught.value)
        assert "IMDb id" in str(caught.value)

    def test_a_show_without_an_imdb_id_is_not_found(self, monkeypatch):
        found = [{"show": {"name": "Something", "externals": {"imdb": None}}}]
        monkeypatch.setattr(eztv, "get_json", lambda url, **kwargs: found)
        with pytest.raises(ApiError) as caught:
            eztv.search("something")
        assert caught.value.status == 404

    def test_a_lookup_that_cannot_answer_is_a_service_error(self, monkeypatch):
        def boom(url, **kwargs):
            raise RuntimeError("no route to host")

        monkeypatch.setattr(eztv, "get_json", boom)
        with pytest.raises(ApiError) as caught:
            eztv.search("the boys")
        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE
        assert "IMDb id" in str(caught.value)


class TestTheEpisodes:
    def test_the_newest_episode_comes_first(self, monkeypatch):
        torrents = [
            torrent("The Boys S05E07", "5", "7"),
            torrent("The Boys S05E08", "5", "8"),
            torrent("The Boys S04E10", "4", "10"),
        ]

        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            return {"torrents_count": len(torrents), "torrents": list(torrents)}

        monkeypatch.setattr(eztv, "get_json", fake)
        titles = [row.title for row in eztv.search("the boys").to_list()]
        assert titles == [
            "The Boys S05E08",
            "The Boys S05E07",
            "The Boys S04E10",
        ]

    def test_releases_of_the_same_episode_are_ordered_by_seeds(self, monkeypatch):
        torrents = [
            torrent("The Boys S05E08 720p", "5", "8", seeds=5),
            torrent("The Boys S05E08 1080p", "5", "8", seeds=50),
        ]

        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            return {"torrents_count": 2, "torrents": list(torrents)}

        monkeypatch.setattr(eztv, "get_json", fake)
        titles = [row.title for row in eztv.search("the boys").to_list()]
        assert titles == [
            "The Boys S05E08 1080p",
            "The Boys S05E08 720p",
        ]

    def test_the_rows_carry_the_imdb_id_for_subtitles(self, monkeypatch):
        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            return {"torrents_count": 1, "torrents": [torrent("An episode")]}

        monkeypatch.setattr(eztv, "get_json", fake)
        row = eztv.search("the boys").to_list()[0]
        # The language is added by the row from the global setting; the id is
        # what this search contributes.
        assert row.subs["imdb"] == "tt1190634"

    def test_a_row_without_a_magnet_is_skipped(self, monkeypatch):
        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            return {
                "torrents_count": 2,
                "torrents": [
                    torrent("No magnet", magnet=None),
                    torrent("Has a magnet"),
                ],
            }

        monkeypatch.setattr(eztv, "get_json", fake)
        titles = [row.title for row in eztv.search("the boys").to_list()]
        assert titles == ["Has a magnet"]

    def test_a_show_with_no_episodes_says_so(self, monkeypatch):
        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            return {"torrents_count": 0, "torrents": []}

        monkeypatch.setattr(eztv, "get_json", fake)
        with pytest.raises(ApiError) as caught:
            eztv.search("the boys")
        assert caught.value.status == 404
        assert "The Boys" in str(caught.value)

    def test_a_failure_from_eztv_is_a_service_error(self, monkeypatch):
        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            raise RuntimeError("timeout")

        monkeypatch.setattr(eztv, "get_json", fake)
        with pytest.raises(ApiError) as caught:
            eztv.search("the boys")
        assert caught.value.status == ApiError._SERVICE_UNAVAILABLE


class TestPaging:
    def test_it_reads_the_pages_the_show_needs(self, monkeypatch):
        pages = {
            1: {"torrents_count": 250, "torrents": [torrent("S05E01")]},
            2: {"torrents_count": 250, "torrents": [torrent("S03E01", "3", "1")]},
            3: {"torrents_count": 250, "torrents": [torrent("S01E01", "1", "1")]},
        }
        calls = []

        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            page = kwargs["params"]["page"]
            calls.append(page)
            return pages[page]

        monkeypatch.setattr(eztv, "get_json", fake)
        rows = eztv.search("the boys").to_list()
        assert calls == [1, 2, 3]
        assert len(rows) == 3

    def test_it_stops_at_the_cap(self, monkeypatch):
        calls = []

        def fake(url, **kwargs):
            if url == eztv._TVMAZE_SEARCH:
                return TVMAZE
            page = kwargs["params"]["page"]
            calls.append(page)
            return {
                "torrents_count": 100000,
                "torrents": [torrent("S05E01")],
            }

        monkeypatch.setattr(eztv, "get_json", fake)
        eztv.search("the boys")
        assert calls == [1, 2, 3, 4, 5]

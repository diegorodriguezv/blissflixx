"""
Play items and the actions attached to them.

The TorrentFilesAction cases are a regression guard. That class used to be
declared twice in lib/playitem; the second declaration shadowed the first and
reported type "showmore", so "View Files..." never reached the torrent/files
endpoint and showed a torrent's files page nothing at all.
"""

import pytest

from lib.api.torrent import TorrentPlayItem
from lib.playitem import (
    ActionList,
    AddSubtitlesAction,
    AddPlaylistAction,
    EditPlaylistItemAction,
    ImdbSearchAction,
    LiveStreamPlayItem,
    MoreEpisodesAction,
    PlayItem,
    PlayItemList,
    PlaylistItem,
    RemoveFromPlaylistAction,
    SearchItem,
    ShowmoreAction,
    ShowMoreItem,
    TorrentFilesAction,
    imdb_query,
)

TORRENT_URL = "http://example.com/movie.torrent?bf_torr_idx=-1"


class TestActionDicts:
    @pytest.mark.parametrize(
        "action,expected_type,expected_label",
        [
            (AddPlaylistAction(), "addplaylist", "Add To Playlist"),
            (AddSubtitlesAction(), "addsubtitles", "Add Subtitles"),
            (
                RemoveFromPlaylistAction(),
                "delplaylistitem",
                "Remove From Playlist",
            ),
            (EditPlaylistItemAction(), "editplaylistitem", "Edit Item"),
        ],
    )
    def test_fixed_actions(self, action, expected_type, expected_label):
        d = action.to_dict()
        assert d["type"] == expected_type
        assert d["label"] == expected_label

    def test_showmore_carries_link_and_title(self):
        d = ShowmoreAction("More", "http://x", "Title").to_dict()
        assert d == {
            "type": "showmore",
            "label": "More",
            "link": "http://x",
            "title": "Title",
        }

    def test_more_episodes_is_a_showmore(self):
        d = MoreEpisodesAction("http://x", "Title").to_dict()
        assert d["type"] == "showmore"
        assert d["label"] == "More Episodes..."


class TestTorrentFilesActionRegression:
    """
    The "View Files..." button. html/tags/itemlist.html dispatches on this type:
    "torrfiles" routes to html/pages/torrfiles.html, which calls torrent/files,
    while "showmore" routes to channels/showmore instead.
    """

    def test_type_is_torrfiles(self):
        d = TorrentFilesAction(TORRENT_URL, "Movie").to_dict()
        assert d["type"] == "torrfiles"

    def test_payload_is_complete(self):
        d = TorrentFilesAction(TORRENT_URL, "Movie").to_dict()
        assert d == {
            "type": "torrfiles",
            "label": "View Files...",
            "link": TORRENT_URL,
            "title": "Movie",
        }

    def test_frontend_dispatch_target_exists(self):
        """
        Guards the other half of the contract: the type this action reports must
        be a type the frontend knows how to handle.
        """
        import pathlib

        itemlist = (
            pathlib.Path(__file__).resolve().parents[1]
            / "html"
            / "tags"
            / "itemlist.html"
        ).read_text()
        assert "case 'torrfiles':" in itemlist
        assert "goRoute('torrfiles'" in itemlist

    def test_no_shadowing_duplicate_class(self):
        """
        TorrentFilesAction must be defined once. A duplicate silently shadows
        the first, which is how the regression happened in the first place.
        """
        import pathlib

        src = (
            pathlib.Path(__file__).resolve().parents[1]
            / "lib"
            / "playitem"
            / "__init__.py"
        ).read_text()
        live = [
            line
            for line in src.splitlines()
            if line.startswith("class TorrentFilesAction")
        ]
        assert len(live) == 1


class TestActionList:
    def test_empty_list_serialises_to_none(self):
        assert ActionList().to_dict() is None

    def test_constructor_seeds_one_action(self):
        al = ActionList(AddPlaylistAction())
        assert not al.empty()
        assert len(al.to_dict()) == 1

    def test_add_appends(self):
        al = ActionList()
        al.add(AddPlaylistAction())
        al.add(ShowmoreAction("L", "u", "t"))
        assert len(al.to_dict()) == 2


class TestPlayItem:
    def test_minimal_item_omits_optional_fields(self):
        d = PlayItem("T", "/img.png", "http://x").to_dict()
        assert d["title"] == "T"
        assert d["img"] == "/img.png"
        assert d["url"] == "http://x"
        for key in ("subtitle", "synopsis", "subs"):
            assert key not in d

    def test_optional_fields_included_when_given(self):
        d = PlayItem(
            "T", "/i.png", "http://x", subtitle="sub", synopsis="syn", subs=None
        ).to_dict()
        assert d["subtitle"] == "sub"
        assert d["synopsis"] == "syn"
        assert "subs" not in d

    def test_add_to_playlist_action_always_present(self):
        types = [
            a["type"] for a in PlayItem("T", "/i", "http://x").to_dict()["actions"]
        ]
        assert "addplaylist" in types

    def test_non_torrent_url_gets_no_view_files_action(self):
        d = PlayItem("T", "/i", "http://example.com/video").to_dict()
        assert "actions" not in d or all(a["type"] != "torrfiles" for a in d["actions"])

    def test_subs_get_default_language(self):
        d = PlayItem("T", "/i", "http://x", subs={"title": "m"}).to_dict()
        assert d["subs"]["lang"] == "eng"
        assert "addsubtitles" in [a["type"] for a in d["actions"]]

    def test_subs_language_follows_setting(self, settings):
        settings.save("subtitles", {"lang": "spa"})
        settings._cache.clear()
        d = PlayItem("T", "/i", "http://x", subs={"title": "m"}).to_dict()
        assert d["subs"]["lang"] == "spa"


class TestTorrentPlayItem:
    def test_stamps_main_file_index(self):
        item = TorrentPlayItem("M", "/i.png", "http://x/movie.torrent")
        assert item.url.endswith("bf_torr_idx=-1")

    def test_includes_view_files_action(self):
        d = TorrentPlayItem("M", "/i.png", "http://x/movie.torrent").to_dict()
        assert "torrfiles" in [a["type"] for a in d["actions"]]


class TestImdbSearch:
    """
    The looking glass on a torrent row.

    The name on the row is a release file name -- quality, source, codec and
    group all in it -- so searching for it finds nothing. The query is built
    from what the name is, not from how it is written.
    """

    @pytest.mark.parametrize(
        "release,query",
        [
            (
                "Spider-Man.Brand.New.Day.2026.1080p.WEBRip.10Bit.DDP5.1.x265-NeoNoir",
                "Spider-Man Brand New Day (2026)",
            ),
            ("Backrooms (2026) [1080p] [WEBRip] [5.1]", "Backrooms (2026)"),
            # An episode has no page of its own: the series is the thing to find.
            ("Some.Show.S01E02.1080p.WEB.H264-GROUP", "Some Show"),
            # With the year, because it tells an original from a remake.
            ("The.Bear.2022.S01E02.1080p.WEB.H264-GROUP", "The Bear (2022)"),
        ],
    )
    def test_the_query_is_the_title_and_not_the_file_name(self, release, query):
        assert imdb_query(release) == query

    def test_a_torrent_row_carries_the_button(self):
        actions = {
            a["type"]: a
            for a in PlayItem(
                "Spider-Man.Brand.New.Day.2026.1080p.WEBRip.x265-NeoNoir",
                "/i",
                "magnet:?xt=urn:btih:" + "A" * 40,
            ).to_dict()["actions"]
        }
        action = actions["imdbsearch"]
        assert action["dest"] == (
            "https://www.imdb.com/find/?q=Spider-Man%20Brand%20New%20Day%20%282026%29"
        )
        # A looking glass rather than another word, and the hover text says what
        # it does because there is no label to read.
        assert action["icon"] == "fa-search"
        assert action["label"] == ""
        assert action["hint"] == "Search IMDb"

    def test_everything_else_does_not(self):
        """
        A channel row's title is already the thing itself; searching IMDb for
        it would be searching for a title that was written to be read.
        """
        d = PlayItem(
            "Some video", "/i", "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        ).to_dict()
        assert all(a["type"] != "imdbsearch" for a in d["actions"])

    def test_frontend_opens_it_in_a_new_tab(self):
        import pathlib

        itemlist = (
            pathlib.Path(__file__).resolve().parents[1]
            / "html"
            / "tags"
            / "itemlist.html"
        ).read_text()
        assert "case 'imdbsearch':" in itemlist
        assert "window.open(action.dest, '_blank'" in itemlist


class TestItemSubclasses:
    def test_livestream(self):
        item = LiveStreamPlayItem("T", "/i", "http://twitch/x")
        assert item.to_dict()["title"] == "T"

    def test_search_item_uses_search_scheme_and_no_actions(self):
        d = SearchItem("query", "/i").to_dict()
        assert d["url"] == "search://query"
        assert "actions" not in d

    def test_showmore_item_uses_showmore_scheme_and_no_actions(self):
        d = ShowMoreItem("Label", "/i", "http://next").to_dict()
        assert d["url"] == "showmore://http://next"
        assert "actions" not in d

    def test_playlist_item_carries_position(self):
        raw = {
            "title": "T",
            "img": "/i",
            "url": "http://x",
            "subtitle": "s",
            "synopsis": "sy",
        }
        d = PlaylistItem(raw, "myplaylist", 3, False).to_dict()
        assert d["playlist"] == "myplaylist"
        assert d["itemnum"] == 3
        assert d["subtitle"] == "s"

    def test_playlist_item_remote_omits_edit_actions(self):
        raw = {"title": "T", "img": "/i", "url": "http://x"}
        remote = [
            a["type"] for a in PlaylistItem(raw, "p", 0, True).to_dict()["actions"]
        ]
        local = [
            a["type"] for a in PlaylistItem(raw, "p", 0, False).to_dict()["actions"]
        ]
        assert "delplaylistitem" not in remote
        assert "delplaylistitem" in local
        assert "editplaylistitem" not in remote
        assert "editplaylistitem" in local


class TestPlayItemList:
    def test_empty(self):
        assert PlayItemList().to_dict() == []

    def test_preserves_insertion_order(self):
        pl = PlayItemList()
        for name in ("a", "b", "c"):
            pl.add(PlayItem(name, "/i", "http://x"))
        assert [i["title"] for i in pl.to_dict()] == ["a", "b", "c"]

    def test_to_list_returns_objects(self):
        pl = PlayItemList()
        item = PlayItem("a", "/i", "http://x")
        pl.add(item)
        assert pl.to_list() == [item]

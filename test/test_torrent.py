"""
Torrent URL helpers.

These back the "bf_torr_idx" query parameter, which is how BlissFlixx records
which file inside a torrent to play. A round trip through these functions has to
be lossless, otherwise playback silently starts the wrong file.
"""

import pytest

from lib.api.torrent import is_torrent_url, set_torridx, torrent2magnet, torrent_files
from lib.chanutils.torrent import is_main, is_torrent, torrent_idx

HASH = "A" * 40


class TestIsTorrent:
    def test_torrent_extension(self):
        assert is_torrent("http://example.com/file.torrent")

    def test_magnet(self):
        assert is_torrent("magnet:?xt=urn:btih:" + HASH)

    def test_html_page_is_not_a_torrent(self):
        assert not is_torrent("http://example.com/watch?v=abc")

    def test_path_ending_in_torrent_ignores_query(self):
        assert is_torrent("http://example.com/x.torrent?bf_torr_idx=2")


class TestSetTorridx:
    def test_adds_param_to_clean_url(self):
        assert set_torridx("http://x/f.torrent", 3) == (
            "http://x/f.torrent?bf_torr_idx=3"
        )

    def test_appends_to_url_with_existing_query(self):
        assert set_torridx("http://x/f.torrent?a=1", 3) == (
            "http://x/f.torrent?a=1&bf_torr_idx=3"
        )

    def test_replaces_existing_index_without_duplicating(self):
        first = set_torridx("http://x/f.torrent", 3)
        second = set_torridx(first, 7)
        assert second == "http://x/f.torrent?bf_torr_idx=7"
        assert second.count("bf_torr_idx=") == 1

    def test_main_index_is_minus_one(self):
        assert set_torridx("http://x/f.torrent") == "http://x/f.torrent?bf_torr_idx=-1"

    def test_preserves_other_params_when_reindexing(self):
        url = set_torridx("http://x/f.torrent?token=abc", 1)
        url = set_torridx(url, 5)
        assert "token=abc" in url
        assert torrent_idx(url) == 5

    def test_works_on_magnet(self):
        magnet = "magnet:?xt=urn:btih:" + HASH
        assert torrent_idx(set_torridx(magnet, 4)) == 4


class TestTorrentIdx:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("http://x/f.torrent", None),
            ("http://x/f.torrent?bf_torr_idx=0", 0),
            ("http://x/f.torrent?a=b&bf_torr_idx=12", 12),
            ("http://x/f.torrent?bf_torr_idx=-1", -1),
        ],
    )
    def test_extract(self, url, expected):
        assert torrent_idx(url) == expected

    def test_is_main_only_for_explicit_minus_one(self):
        assert is_main("http://x/f.torrent?bf_torr_idx=-1")
        assert not is_main("http://x/f.torrent?bf_torr_idx=0")
        assert not is_main("http://x/f.torrent")


class TestIsTorrentUrl:
    def test_detects_marker(self):
        assert is_torrent_url("http://x/f.torrent?bf_torr_idx=1")

    def test_rejects_plain_url(self):
        assert not is_torrent_url("http://x/f.torrent")


class TestFileList:
    """
    What a torrent's "View Files" page shows.

    It used to show everything -- the .nfo, the .jpg, the subtitles -- which
    made a page of a pack's episodes into a directory listing. Only what can be
    watched is listed now, and the files that are subtitles come back as
    choices on the row rather than as rows of their own.
    """

    FILES = [
        ("Below.S01E01.1080p.mkv", "2.1GB"),
        ("Below.S01E01.1080p.eng.srt", "84KB"),
        ("Below.S01E02.1080p.mkv", "2.0GB"),
        ("Below.S01E02.1080p.eng.srt", "79KB"),
        ("Subs/English.srt", "12KB"),
        ("readme.nfo", "2KB"),
        ("cover.jpg", "120KB"),
    ]

    @pytest.fixture
    def items(self, monkeypatch):
        from lib.api import torrent as torrent_api

        monkeypatch.setattr(torrent_api, "torrent_files", lambda link: self.FILES)
        return torrent_api.showmore("magnet:?xt=urn:btih:" + HASH).to_dict()

    def test_videos_and_their_subtitles_are_listed(self, items):
        """
        The subtitles are shown so the page says what the torrent carries, and
        nothing else is: the .nfo and the artwork are not what this page is for.
        """
        assert [i["title"] for i in items] == [
            "Below.S01E01.1080p.mkv",
            "Below.S01E01.1080p.eng.srt",
            "Below.S01E02.1080p.mkv",
            "Below.S01E02.1080p.eng.srt",
            "Subs/English.srt",
        ]

    def test_a_subtitle_row_has_no_link_and_no_buttons(self, items):
        row = [i for i in items if i["title"].endswith(".srt")][0]
        assert row["url"] == ""
        assert "actions" not in row

    def test_the_torrents_own_index_is_kept(self, items):
        """Showing fewer rows must not renumber what is played."""
        videos = [i for i in items if i["url"]]
        assert videos[0]["url"].endswith("bf_torr_idx=0")
        assert videos[1]["url"].endswith("bf_torr_idx=2")

    def test_a_file_gets_the_subtitles_that_name_it(self, items):
        """
        Its own first, and the ones that name no episode at all -- "Subs/
        English.srt" is one subtitle for everything, and dropping it because it
        does not carry this episode's name would hide the only English one.
        """
        first = items[0]["subs"]["torrent"]["files"]
        assert [f["name"] for f in first] == [
            "Below.S01E01.1080p.eng.srt",
            "Subs/English.srt",
        ]
        assert first[0]["idx"] == 1

    def test_each_episode_gets_its_own(self, items):
        second = [i for i in items if i["title"] == "Below.S01E02.1080p.mkv"][0]
        names = [f["name"] for f in second["subs"]["torrent"]["files"]]
        assert names == ["Below.S01E02.1080p.eng.srt", "Subs/English.srt"]

    def test_a_films_language_subtitles_are_not_hidden_by_the_one_that_matches(
        self, monkeypatch
    ):
        """
        A release names one subtitle after the film and the rest by language.
        Matching on the name alone offered one of four, which is how a film with
        four subtitle files appeared to have one.
        """
        from lib.api import torrent as torrent_api

        monkeypatch.setattr(
            torrent_api,
            "torrent_files",
            lambda link: [
                ("Film.2026.1080p.mp4", "2.9GB"),
                ("Film.2026.1080p.srt", "123KB"),
                ("British (SDH).eng.srt", "170KB"),
                ("English.srt", "129KB"),
            ],
        )
        items = torrent_api.showmore("magnet:?xt=urn:btih:" + HASH).to_dict()
        names = [f["name"] for f in items[0]["subs"]["torrent"]["files"]]
        # Most probable first: the release's own, then a plain track, then the
        # one made for people who want the sound described. All three are there.
        assert names == ["Film.2026.1080p.srt", "English.srt", "British (SDH).eng.srt"]

    def test_another_episodes_subtitles_are_left_out(self, monkeypatch):
        """
        The same generosity must not turn a season pack into sixty choices.
        """
        from lib.api import torrent as torrent_api

        monkeypatch.setattr(
            torrent_api,
            "torrent_files",
            lambda link: [
                ("Show.S01E01.mkv", "1GB"),
                ("Show.S01E01.eng.srt", "50KB"),
                ("Show.S01E02.mkv", "1GB"),
                ("Show.S01E02.eng.srt", "50KB"),
                ("Show.S01E02.spa.srt", "50KB"),
            ],
        )
        items = torrent_api.showmore("magnet:?xt=urn:btih:" + HASH).to_dict()
        names = [f["name"] for f in items[0]["subs"]["torrent"]["files"]]
        assert names == ["Show.S01E01.eng.srt"]

    def test_the_row_offers_the_subtitle_search(self, items):
        types = [a["type"] for a in items[0]["actions"]]
        assert "addsubtitles" in types

    def test_a_torrent_without_subtitles_offers_no_action(self, monkeypatch):
        from lib.api import torrent as torrent_api

        monkeypatch.setattr(
            torrent_api, "torrent_files", lambda link: [("Film.mkv", "1.2GB")]
        )
        items = torrent_api.showmore("magnet:?xt=urn:btih:" + HASH).to_dict()
        assert "subs" not in items[0]
        assert "addsubtitles" in [a["type"] for a in items[0]["actions"]]

    def test_an_unmatched_subtitle_is_still_offered(self, monkeypatch):
        """
        A pack can keep its subtitles in a folder with language names, where
        nothing pairs with the episode. Offering all of them beats offering
        none: the names say which is which.
        """
        from lib.api import torrent as torrent_api

        monkeypatch.setattr(
            torrent_api,
            "torrent_files",
            lambda link: [
                ("Show.S01E01.mkv", "1GB"),
                ("Subs/English.srt", "50KB"),
                ("Subs/Spanish.srt", "50KB"),
            ],
        )
        items = torrent_api.showmore("magnet:?xt=urn:btih:" + HASH).to_dict()
        names = [f["name"] for f in items[0]["subs"]["torrent"]["files"]]
        assert names == ["Subs/English.srt", "Subs/Spanish.srt"]


class TestTorrent2Magnet:
    def test_keeps_a_magnets_own_trackers_and_gains_ours(self):
        """
        The magnet's trackers are the uploader's, not necessarily the working
        ones; both sets are kept, and a second conversion adds nothing.
        """
        from urllib.parse import quote

        from lib.api.torrent import TRACKERS

        own = "udp://old.example:1/announce"
        magnet = "magnet:?xt=urn:btih:" + HASH + "&tr=" + quote(own, safe="")
        result = torrent2magnet(magnet)
        assert result.startswith(magnet)
        assert quote(own, safe="") in result
        for tracker in TRACKERS:
            assert "tr=" + quote(tracker, safe="") in result
        assert torrent2magnet(result) == result

    def test_builds_magnet_from_hash_with_trackers(self):
        magnet = torrent2magnet(HASH)
        assert magnet.startswith("magnet:?xt=urn:btih:")
        assert HASH in magnet
        assert "&tr=" in magnet

    def test_passes_through_input_without_a_hash(self):
        assert torrent2magnet("http://example.com/not-a-hash") == (
            "http://example.com/not-a-hash"
        )


class TestTorrentFiles:
    """
    Listing a torrent's files without playing it.

    The torrent process answers with one line of JSON, so the parser has no
    column offsets to depend on: it reads the line it printed and nothing else.
    subprocess is stubbed -- the process needs a swarm and is exercised on the
    box.
    """

    def _listing(self, monkeypatch, files, returncode=0, output=None):
        import json as _json
        import types

        line = "bf-files: " + _json.dumps(files)
        seen = {}

        def fake_run(cmd, **kwargs):
            seen["cmd"] = cmd
            text = output if output is not None else line
            return types.SimpleNamespace(
                returncode=returncode, stdout=text.encode("utf-8")
            )

        monkeypatch.setattr("lib.player.ltproc.subprocess.run", fake_run)
        return seen

    def test_paths_and_sizes_come_back_in_the_torrents_order(self, monkeypatch):
        seen = self._listing(
            monkeypatch,
            [
                {"path": "Pack/E01.mkv", "size": 1000},
                {"path": "Pack/E01.eng.srt", "size": 50},
            ],
        )
        assert torrent_files(HASH) == [("Pack/E01.mkv", 1000), ("Pack/E01.eng.srt", 50)]
        assert "--list" in seen["cmd"]
        assert seen["cmd"][2].startswith("magnet:?xt=urn:btih:")

    def test_the_torrent_is_converted_to_a_magnet_first(self, monkeypatch):
        seen = self._listing(monkeypatch, [])
        torrent_files(HASH)
        assert seen["cmd"][2].startswith("magnet:?xt=urn:btih:")
        assert HASH in seen["cmd"][2]

    def test_a_failure_carries_the_command(self, monkeypatch):
        self._listing(monkeypatch, [], returncode=3, output="bf-bt: error no metadata after 45s")
        with pytest.raises(RuntimeError) as raised:
            torrent_files(HASH)
        message = str(raised.value)
        assert "Unable to retrieve torrent files" in message
        assert "--list" in message
        assert "no metadata" in message

    def test_a_timeout_says_so(self, monkeypatch):
        import subprocess

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 90)

        monkeypatch.setattr("lib.player.ltproc.subprocess.run", fake_run)
        with pytest.raises(RuntimeError, match="No answer listing this torrent"):
            torrent_files(HASH)

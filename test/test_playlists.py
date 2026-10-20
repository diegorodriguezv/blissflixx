import json
import pathlib

import pytest

from lib.api import playlists

#: The first three lines of iptv-org's news list, which is what the parser was
#: written against: 1017 channels, 61 #EXTVLCOPT lines, and 7 empty logos.
HEADER = "#EXTM3U"
TWO_GB = (
    '#EXTINF:-1 tvg-id="2GB.au@SD" tvg-logo="https://i.ibb.co/jwM8DFG/2GB-1.png"'
    ' group-title="News",2GB (1080p)'
)
TWO_GB_URL = "https://2gblive.akamaized.net/hls/live/2033805/2GB/index.m3u8"


class TestReadingAnM3u:
    def test_a_channel_becomes_an_item_with_its_logo_and_group(self):
        items = playlists._parse_m3u("\n".join([HEADER, TWO_GB, TWO_GB_URL]))
        assert items == [
            {
                "title": "2GB (1080p)",
                "img": "https://i.ibb.co/jwM8DFG/2GB-1.png",
                "url": TWO_GB_URL,
                "subtitle": "News",
            }
        ]

    def test_a_line_with_no_attributes_still_has_a_title(self):
        items = playlists._parse_m3u(
            "\n".join([HEADER, "#EXTINF:-1,Channel One", TWO_GB_URL])
        )
        assert [i["title"] for i in items] == ["Channel One"]

    def test_a_title_keeps_every_comma_after_the_attributes(self):
        """
        The comma that separates the title comes after the last attribute, not
        the first one: a channel called "News, live" is one channel.
        """
        items = playlists._parse_m3u(
            "\n".join([HEADER, '#EXTINF:-1 group-title="News",News, live', TWO_GB_URL])
        )
        assert [i["title"] for i in items] == ["News, live"]

    def test_windows_line_endings_and_a_byte_order_mark(self):
        text = "\ufeff" + "#EXTM3U\r\n" + TWO_GB + "\r\n" + TWO_GB_URL + "\r\n"
        assert [i["url"] for i in playlists._parse_m3u(text)] == [TWO_GB_URL]

    def test_stream_options_are_not_mistaken_for_urls(self):
        """
        #EXTVLCOPT carries a user agent or a referrer for the stream below it.
        Read as a URL it would be an item that cannot play, and the real one
        would be dropped with it.
        """
        text = "\n".join(
            [
                HEADER,
                TWO_GB,
                "#EXTVLCOPT:http-user-agent=Mozilla/5.0",
                "#EXTVLCOPT:http-referrer=https://example.com/",
                TWO_GB_URL,
            ]
        )
        assert [i["url"] for i in playlists._parse_m3u(text)] == [TWO_GB_URL]

    def test_an_empty_logo_gets_the_default_icon(self):
        """
        PlaylistItem reads img out of the dict and raises without one, so an
        item with no picture is still an item with a picture.
        """
        text = "\n".join([HEADER, '#EXTINF:-1 tvg-logo="",Channel One', TWO_GB_URL])
        assert playlists._parse_m3u(text)[0]["img"] == playlists._DEFAULT_IMG

    def test_a_list_of_bare_urls_is_still_a_list(self):
        items = playlists._parse_m3u("\n".join([HEADER, TWO_GB_URL]))
        assert [i["title"] for i in items] == ["index.m3u8"]

    def test_a_relative_url_is_resolved_against_the_list(self):
        items = playlists._parse_m3u(
            "\n".join([HEADER, "#EXTINF:-1,One", "streams/one.m3u8"]),
            "https://example.com/lists/news.m3u",
        )
        assert [i["url"] for i in items] == [
            "https://example.com/lists/streams/one.m3u8"
        ]

    def test_a_stream_this_player_cannot_play_is_not_listed(self):
        """
        udp, rtp, srt and rtsp do not go through the yt-dlp pipeline. Listed,
        they look exactly like the channels that work until somebody presses
        one, which is the rule the Reddit feed already applies to a channel
        link.
        """
        text = "\n".join(
            [
                HEADER,
                "#EXTINF:-1,UDP",
                "udp://239.0.0.1:1234",
                "#EXTINF:-1,SRT",
                "srt://example.com:9000",
                "#EXTINF:-1,One",
                TWO_GB_URL,
            ]
        )
        assert [i["title"] for i in playlists._parse_m3u(text)] == ["One"]

    def test_an_hls_manifest_is_refused_with_the_place_to_use_it(self):
        text = "\n".join(
            [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                "#EXT-X-TARGETDURATION:6",
                "#EXTINF:6.0,",
                "segment1.ts",
            ]
        )
        with pytest.raises(playlists.ApiError) as exc:
            playlists._parse_m3u(text)
        assert "HLS stream" in str(exc.value)
        assert "Play URL" in str(exc.value)

    def test_a_web_page_is_refused_rather_than_read_as_a_list(self):
        """
        A Dropbox or Google Drive share link serves a page around the file.
        Saying so beats an empty playlist nobody can explain.
        """
        with pytest.raises(playlists.ApiError) as exc:
            playlists._parse_m3u("<!DOCTYPE html>\n<html><body>...</body></html>")
        assert "web page" in str(exc.value)

    def test_an_empty_reply_is_refused(self):
        with pytest.raises(playlists.ApiError) as exc:
            playlists._parse_m3u("#EXTM3U\n")
        assert "M3U" in str(exc.value)


class _Reply:
    """The parts of a requests response this code reads."""

    def __init__(self, text, status=200):
        self.content = text.encode("utf-8")
        self.status_code = status


class TestFetchingOne:
    @pytest.fixture
    def fetch(self, monkeypatch):
        """Answer requests.get with one reply, and count the calls."""

        def _fetch(text, status=200, error=None):
            calls = []

            def _get(url, timeout=None):
                calls.append((url, timeout))
                if error is not None:
                    raise error
                return _Reply(text, status)

            monkeypatch.setattr(playlists.requests, "get", _get)
            return calls

        return _fetch

    def test_the_title_comes_from_the_file_name(self, fetch):
        fetch("\n".join([HEADER, TWO_GB, TWO_GB_URL]))
        playlist = playlists._fetch_remote("https://example.com/lists/news.m3u")
        assert playlist["title"] == "news [REMOTE]"
        assert playlist["url"] == "https://example.com/lists/news.m3u"
        assert [i["url"] for i in playlist["items"]] == [TWO_GB_URL]

    def test_a_playlist_line_wins_over_the_file_name(self, fetch):
        fetch("\n".join(["#EXTM3U", "#PLAYLIST:World News", TWO_GB, TWO_GB_URL]))
        playlist = playlists._fetch_remote("https://example.com/lists/news.m3u")
        assert playlist["title"] == "World News [REMOTE]"

    def test_a_missing_list_says_so(self, fetch):
        fetch("not there", status=404)
        with pytest.raises(playlists.ApiError) as exc:
            playlists._fetch_remote("https://example.com/lists/news.m3u")
        assert "404" in str(exc.value)

    def test_a_source_that_does_not_answer_does_not_hang_the_page(self, fetch):
        """
        The refresh runs on read, inside get(), which list() calls for every
        playlist -- so the timeout is what keeps one dead remote from taking the
        playlists page with it.
        """
        calls = fetch("", error=playlists.requests.Timeout("timed out"))
        with pytest.raises(playlists.ApiError) as exc:
            playlists._fetch_remote("https://example.com/lists/news.m3u")
        assert exc.value.status == playlists.ApiError._SERVICE_UNAVAILABLE
        assert calls[0][1] == playlists.REMOTE_TIMEOUT


class TestAListOnDisk:
    @pytest.fixture
    def plists(self, tmp_path, monkeypatch):
        path = tmp_path / "playlists"
        path.mkdir()
        monkeypatch.setattr(playlists, "PLIST_PATH", str(path))
        return playlists

    def _write(self, plists, name, playlist, age=0):
        """A playlist file as the real ones are written, optionally back-dated."""
        path = pathlib.Path(plists.PLIST_PATH) / (name + ".bfpl")
        path.write_text(json.dumps(playlist))
        if age:
            import os
            import time

            old = time.time() - age
            os.utime(str(path), (old, old))
        return path

    def test_a_refresh_replaces_the_items(self, plists, monkeypatch):
        self._write(
            plists,
            "news_[REMOTE]",
            {
                "title": "news [REMOTE]",
                "url": "https://example.com/news.m3u",
                "items": [
                    {"title": "old", "img": "/x.svg", "url": "https://example.com/old"}
                ],
            },
            age=plists.REMOTE_REFRESH_INT + 60,
        )
        monkeypatch.setattr(
            plists,
            "_fetch_remote",
            lambda url: {
                "title": "news [REMOTE]",
                "url": url,
                "items": [{"title": "new", "img": "/x.svg", "url": TWO_GB_URL}],
            },
        )
        playlist = plists.get("news_[REMOTE]")
        assert [i["title"] for i in playlist["items"]] == ["new"]

    def test_a_failed_refresh_keeps_the_copy_that_worked(self, plists, monkeypatch):
        """
        This used to raise, and the exception escaped through list() -- one
        unreachable URL emptied the whole playlists page while the last good
        copy sat on disk.
        """
        self._write(
            plists,
            "news_[REMOTE]",
            {
                "title": "news [REMOTE]",
                "url": "https://example.com/news.m3u",
                "items": [{"title": "cached", "img": "/x.svg", "url": TWO_GB_URL}],
            },
            age=plists.REMOTE_REFRESH_INT + 60,
        )

        calls = []

        def _fail(url):
            calls.append(url)
            raise playlists.ApiError("Could not fetch that playlist: timed out")

        monkeypatch.setattr(plists, "_fetch_remote", _fail)
        playlist = plists.get("news_[REMOTE]")
        assert [i["title"] for i in playlist["items"]] == ["cached"]
        # A failure counts as an attempt, so the page does not wait out the
        # timeout again on the next read.
        plists.get("news_[REMOTE]")
        assert len(calls) == 1

    def test_a_fresh_list_is_not_fetched_again(self, plists, monkeypatch):
        self._write(
            plists,
            "news_[REMOTE]",
            {
                "title": "news [REMOTE]",
                "url": "https://example.com/news.m3u",
                "items": [{"title": "cached", "img": "/x.svg", "url": TWO_GB_URL}],
            },
        )
        called = []
        monkeypatch.setattr(plists, "_fetch_remote", lambda url: called.append(url))
        plists.get("news_[REMOTE]")
        assert called == []

    def test_a_remote_list_is_stored_and_read_back_read_only(self, plists, monkeypatch):
        """
        The whole import, through the same two functions the buttons reach:
        fetched, saved under its title, and read back with no edit actions --
        because the list is the source, not this disk.
        """
        monkeypatch.setattr(
            plists.requests,
            "get",
            lambda url, timeout=None: _Reply("\n".join([HEADER, TWO_GB, TWO_GB_URL])),
        )
        playlist = plists.new_remote("https://example.com/lists/news.m3u")
        assert playlist["plid"] == "news_[REMOTE]"
        assert [i["title"] for i in playlist["items"]] == ["2GB (1080p)"]
        assert playlist["items"][0]["playlist"] == "news_[REMOTE]"
        labels = [a["label"] for a in playlist["items"][0]["actions"]]
        assert "Remove From Playlist" not in labels

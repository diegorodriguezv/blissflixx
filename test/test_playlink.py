import pytest
import requests

from lib.api import playlink
from lib.api.common import ApiError
from lib.chanutils import chanutils

PLAYLIST = "https://2gblive.akamaized.net/hls/live/2033805/2GB/index.m3u8"


class _Reply:
    """The parts of a requests response UrlInfo reads."""

    def __init__(self, body):
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        self.raw = self

    def read(self, size, decode_content=False):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class TestReadingAPage:
    @pytest.fixture
    def reply(self, monkeypatch):
        def _reply(body):
            monkeypatch.setattr(
                chanutils.requests, "get", lambda url, **kw: _Reply(body)
            )
            return chanutils.UrlInfo("http://example.com/")

        return _reply

    def test_a_page_gives_up_its_title(self, reply):
        info = reply("<html><head><title>A Page</title></head></html>")
        assert info.get_html_title() == "A Page"

    def test_a_stream_has_no_title_to_give(self, reply):
        """
        Pasting an .m3u8 into Play URL used to index straight through the
        missing <title> and come back as a 500 with a traceback.
        """
        info = reply("#EXTM3U\n#EXT-X-VERSION:3\n")
        assert info.get_html_title() is None

    def test_neither_does_an_empty_reply(self, reply):
        assert reply("").get_html_title() is None


class TestPlayUrl:
    @pytest.fixture
    def no_page(self, monkeypatch):
        """A reply with no title, as a stream's is."""

        class _Info:
            def __init__(self, url):
                pass

            def get_html_title(self):
                return None

        monkeypatch.setattr(playlink, "UrlInfo", _Info)

    def test_a_stream_is_named_after_its_path(self, no_page):
        items = playlink.item(PLAYLIST)
        assert items[0]["title"] == "index.m3u8"
        assert items[0]["url"] == PLAYLIST

    def test_a_url_that_does_not_answer_is_a_message_not_a_traceback(self, monkeypatch):
        def _boom(url):
            raise requests.ConnectionError("no route to host")

        monkeypatch.setattr(playlink, "UrlInfo", _boom)
        with pytest.raises(ApiError) as exc:
            playlink.item(PLAYLIST)
        assert "Could not fetch" in str(exc.value)
        assert exc.value.status == ApiError._SERVICE_UNAVAILABLE

    def test_a_page_keeps_its_title(self, monkeypatch):
        class _Info:
            def __init__(self, url):
                pass

            def get_html_title(self):
                return "Some Page"

        monkeypatch.setattr(playlink, "UrlInfo", _Info)
        assert playlink.item("http://example.com/")[0]["title"] == "Some Page"

"""
URL classification in lib/player/ythelper.

These regexes decide whether a stream is downloaded to disk first or piped
straight to omxplayer, so a false negative means a video silently buffers to a
file instead of playing immediately.
"""

import pytest

from lib.player.ythelper import get_format, skip_download


class TestSkipDownload:
    """
    skip_download() returning True means "don't download, pipe it". The naming
    reads backwards at call sites, which is why each direction is asserted here.
    """

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.itv.com/watch",
            "http://www.itv.com/btv/abc",
        ],
    )
    def test_itv_streams_are_downloaded_first(self, url):
        # ITV_URL is in DL_URLS, so these are downloaded rather than piped.
        assert skip_download(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://openload.co/f/abc123",
            "https://openload.link/embed/abc123",
            "https://oload.tv/f/XYZ",
        ],
    )
    def test_openload_is_downloaded_first(self, url):
        assert skip_download(url) is False

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://vimeo.com/12345",
            "http://example.com/video.mp4",
            "magnet:?xt=urn:btih:" + "A" * 40,
        ],
    )
    def test_ordinary_urls_are_piped(self, url):
        assert skip_download(url) is True

    def test_bbc_is_deliberately_piped(self):
        """
        BBC_URL is commented out of DL_URLS with no explanation. Assert the
        current behaviour so that if it is ever re-enabled this test fails and
        the change is deliberate rather than accidental.
        """
        assert skip_download("https://www.bbc.co.uk/iplayer/episode/b0000001") is True


class TestGetFormat:
    def test_youtube_forces_mp4(self):
        assert get_format("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "(mp4)"

    def test_vimeo_forces_mp4(self):
        assert get_format("https://vimeo.com/12345") == "(mp4)"

    def test_bbc_caps_below_720p(self):
        assert get_format("https://www.bbc.co.uk/iplayer/episode/b0000001") == (
            "best[height<720]"
        )

    def test_unknown_host_has_no_format_filter(self):
        assert get_format("http://example.com/video.mp4") is None

    def test_youtube_naked_id(self):
        assert get_format("dQw4w9WgXcQ") == "(mp4)"

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
        ],
    )
    def test_youtube_url_shapes(self, url):
        assert get_format(url) == "(mp4)"

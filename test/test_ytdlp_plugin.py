"""
The Rotten Tomatoes plugin for yt-dlp.

yt-dlp's own extractor for rottentomatoes.com broke between its 2026.08.19
and 2026.10.26 releases: the script id it looks for was made into
"heroVideos" or "videos" only, while the movie pages serve it as "ideos" --
a name the working version matched by making the prefix optional. The
fallback that then tried the /videos page was deleted in the same change, so
every film came back as "Unable to extract data" and the channel's rows could
not be played at all.

The fix rides with the checkout as a plugin rather than in site-packages,
because yt-dlp is deliberately unpinned: patching the installed file would be
undone by the next update, and the player already runs yt-dlp from here,
where the plugin is picked up for free. When upstream takes the fix, deleting
the plugin directory is the whole reversion.
"""

import pytest

PLUGIN = "yt_dlp_plugins/extractor/rottentomatoes.py"


def _source():
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / PLUGIN).read_text()


def _module():
    """The plugin's extractor, imported the way yt-dlp imports it."""
    import importlib

    return importlib.import_module("yt_dlp_plugins.extractor.rottentomatoes")


class TestThePlugin:
    def test_the_script_id_prefix_is_optional_again(self):
        """
        The broken releases require "heroVideos" or "videos"; the pages serve
        the script with an id of "ideos", which only the optional prefix
        matches. This is the one character the upstream change dropped.
        """
        source = _source()
        assert "(?:heroV|v)?ideos" in source
        # And not the broken form.
        assert "(?:heroV|v)ideos" not in source

    def test_the_videos_page_fallback_is_back(self):
        """The same upstream change deleted the second place to look."""
        source = _source()
        assert "except ExtractorError" in source
        assert "/videos" in source

    def test_the_extractor_is_importable_the_way_yt_dlp_loads_it(self):
        module = _module()
        ie = module.RottenTomatoesIE
        assert ie.IE_NAME == "RottenTomatoes"
        assert ie.suitable("https://www.rottentomatoes.com/m/madden") is True

    def test_the_movie_page_is_a_playlist_of_trailers_to_the_player(self):
        """--no-playlist takes the first one; the extractor must offer them."""
        ie = _module().RottenTomatoesIE
        assert ie.suitable("https://www.rottentomatoes.com/m/madden/trailers") is True


class TestUpstream:
    @pytest.mark.network
    def test_the_plugin_extracts_a_real_film(self):
        """The whole point: a movie page comes back with a stream to play."""
        import yt_dlp

        with yt_dlp.YoutubeDL({"quiet": True, "simulate": True}) as ydl:
            info = ydl.extract_info(
                "https://www.rottentomatoes.com/m/madden", download=False
            )
        assert info, "extraction returned nothing"
        entries = info.get("entries") or [info]
        url = entries[0].get("url")
        assert url and ".m3u8" in url, "the trailer's stream did not come out"

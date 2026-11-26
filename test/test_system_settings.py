"""
Installation-wide options.

The first is the name on the bar and in the browser tab. It is stored per
installation in data/settings/system, with the built-in name as the fallback.
"""

import pytest

from blissflixx.api import system
from blissflixx.api.common import ApiError


class TestTheTitle:
    def test_the_default_is_blissflixx(self, settings):
        assert system.settings()["title"] == "BlissFlixx"

    def test_it_can_be_changed(self, settings):
        answer = system.set_settings({"title": "blissflixx rpi64"})
        assert answer["title"] == "blissflixx rpi64"
        settings._cache.clear()
        assert settings.load("system")["title"] == "blissflixx rpi64"

    def test_an_empty_title_means_the_default(self, settings):
        answer = system.set_settings({"title": "   "})
        assert answer["title"] == "BlissFlixx"

    def test_it_is_trimmed(self, settings):
        answer = system.set_title("  Living room  ")
        assert answer["title"] == "Living room"

    def test_a_title_that_is_too_long_is_refused(self, settings):
        with pytest.raises(ApiError):
            system.set_settings({"title": "x" * 81})

    def test_unknown_options_are_ignored(self, settings):
        answer = system.set_settings({"title": "One", "nonsense": True})
        assert answer["title"] == "One"
        settings._cache.clear()
        assert "nonsense" not in settings.load("system")

    def test_a_value_that_is_not_a_mapping_is_refused(self, settings):
        with pytest.raises(ApiError):
            system.set_settings(["not", "a", "mapping"])


class TestTheInterface:
    def test_the_options_page_has_a_title_field(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "sysoptions.html").read_text()
        assert 'name="f_title"' in page
        assert "set_settings" in page
        assert "se_title_changed" in page

    def test_the_bar_reads_the_title_rather_than_writing_it_in(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        bar = (root / "html" / "tags" / "topnav.html").read_text()
        assert "{ appTitle }" in bar
        assert "BLISSFLIXX" not in bar
        assert "document.title = title" in bar
        assert "se_title_changed" in bar

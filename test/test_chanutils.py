"""
Pure parsing helpers in lib.chanutils.

These are the functions that turn scraped text into the structures the rest of
BlissFlixx consumes, so they are worth pinning down: a change to a regex here
silently empties a channel's feed rather than raising.
"""

import pytest

from lib.chanutils import (
    byte_size,
    movie_title_year,
    number_commas,
    replace_entity,
    series_season_episode,
)
from lib.chanutils.chanutils import (
    convert_date,
    convert_duration,
    get_attr,
    get_text,
    get_text_content,
    select_all,
    select_one,
)


class TestMovieTitleYear:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("The Matrix (1999)", {"title": "The Matrix", "year": 1999}),
            ("The Matrix 1999 1080p", {"title": "The Matrix", "year": 1999}),
            (
                "Some.Movie.2019.1080p.BluRay",
                {"title": "Some Movie", "year": 2019},
            ),
            ("No Year Here", {"title": "No Year Here"}),
        ],
    )
    def test_extracts(self, raw, expected):
        assert movie_title_year(raw) == expected

    def test_title_stops_at_the_year(self):
        """
        Release-group and quality tags after the year are dropped, not kept.
        The title is used to look subtitles up, so trailing junk is removed.
        """
        assert movie_title_year("The Matrix 1999 1080p BluRay x264-GRP") == {
            "title": "The Matrix",
            "year": 1999,
        }

    def test_returns_only_title_when_no_year(self):
        result = movie_title_year("Documentary About Things")
        assert "year" not in result

    def test_dots_become_spaces(self):
        assert movie_title_year("a.b.c")["title"] == "a b c"


class TestSeriesSeasonEpisode:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Some Show S02E05", {"series": "Some Show", "season": 2, "episode": 5}),
            (
                "Some.Show.S10E23.1080p",
                {"series": "Some Show", "season": 10, "episode": 23},
            ),
            ("No Markers", {"series": "No Markers"}),
        ],
    )
    def test_extracts(self, raw, expected):
        assert series_season_episode(raw) == expected

    def test_series_stops_at_the_episode_marker(self):
        assert series_season_episode("Some Show S02E05 1080p x264-GRP") == {
            "series": "Some Show",
            "season": 2,
            "episode": 5,
        }

    def test_season_and_episode_are_integers(self):
        r = series_season_episode("Show S01E02")
        assert isinstance(r["season"], int)
        assert isinstance(r["episode"], int)

    def test_requires_two_digit_season_and_episode(self):
        assert "season" not in series_season_episode("Show S1E2")


class TestConvertDuration:
    @pytest.mark.parametrize(
        "iso,expected",
        [
            ("PT4M13S", "4:13"),
            ("PT1H2M3S", "1:02:03"),
            ("PT59S", "0:59"),
            ("PT5M", "5:00"),
            ("PT1H", "1:00:00"),
            ("PT1H30M", "1:30:00"),
        ],
    )
    def test_formats(self, iso, expected):
        assert convert_duration(iso) == expected

    def test_zero_padding_on_single_digits(self):
        assert convert_duration("PT1H2M3S") == "1:02:03"


class TestConvertDate:
    @pytest.mark.parametrize(
        "iso,expected",
        [
            ("2020-01-02", "2020-01-02"),
            ("2020-01-02T00:00:00", "2020-01-02"),
            ("2020-12-31T23:59:59", "2020-12-31"),
        ],
    )
    def test_iso_date_to_display(self, iso, expected):
        assert convert_date(iso) == expected

    def test_rejects_unpadded_iso(self):
        """
        datetime.fromisoformat requires zero-padded fields, so a malformed
        value raises rather than being silently mangled. Callers get the
        traceback in the API error response.
        """
        with pytest.raises(ValueError):
            convert_date("2020-1-2T00:00:00")


class TestByteSize:
    @pytest.mark.parametrize(
        "num,expected",
        [
            (0, "0.0 B"),
            (512, "512.0 B"),
            (1024, "1.0 KB"),
            (1536, "1.5 KB"),
            (1024**2, "1.0 MB"),
            (1024**3, "1.0 GB"),
        ],
    )
    def test_formats(self, num, expected):
        assert byte_size(num) == expected

    def test_negative_keeps_sign(self):
        assert byte_size(-1024).startswith("-")

    def test_custom_suffix(self):
        assert byte_size(1024, "B").endswith("B")


class TestReplaceEntity:
    def test_named_entity(self):
        assert replace_entity("a &amp; b") == "a & b"

    def test_numeric_entity(self):
        assert replace_entity("&#65;") == "A"

    def test_hex_entity(self):
        assert replace_entity("&#x41;") == "A"

    def test_leaves_plain_text_alone(self):
        assert replace_entity("plain text") == "plain text"


class TestNumberCommas:
    @pytest.mark.parametrize(
        "num,expected",
        [(0, "0"), (999, "999"), (1000, "1,000"), (1234567, "1,234,567")],
    )
    def test_formats(self, num, expected):
        assert number_commas(num) == expected

    def test_negative(self):
        assert number_commas(-1234) == "-1,234"

    def test_non_numeric_returns_zero(self):
        assert number_commas("abc") == "0"


class TestLxmlHelpers:
    """
    These take lxml elements, so a tiny hand-built tree stands in for scraped
    HTML. Verifying against a live page would make the suite depend on the
    internet and on sites that change.
    """

    def test_select_one_returns_first_match(self):
        import lxml.html

        doc = lxml.html.fromstring("<div><a>1</a><a>2</a></div>")
        el = select_one(doc, "a")
        assert el.text == "1"

    def test_select_one_returns_none_when_absent(self):
        import lxml.html

        doc = lxml.html.fromstring("<div><b>x</b></div>")
        assert select_one(doc, "a") is None

    def test_select_all_returns_list(self):
        import lxml.html

        doc = lxml.html.fromstring("<div><a>1</a><a>2</a></div>")
        assert [e.text for e in select_all(doc, "a")] == ["1", "2"]

    def test_get_attr(self):
        import lxml.html

        doc = lxml.html.fromstring('<a href="http://x">t</a>')
        assert get_attr(select_one(doc, "a"), "href") == "http://x"

    def test_get_attr_on_none_is_none(self):
        assert get_attr(None, "href") is None

    def test_get_text_strips(self):
        import lxml.html

        doc = lxml.html.fromstring("<a>  hi  </a>")
        assert get_text(select_one(doc, "a")) == "hi"

    def test_get_text_on_empty_element_is_none(self):
        import lxml.html

        doc = lxml.html.fromstring("<a></a>")
        assert get_text(select_one(doc, "a")) is None

    def test_get_text_content_includes_children(self):
        import lxml.html

        doc = lxml.html.fromstring("<div><b>bold</b> text</div>")
        assert get_text_content(select_one(doc, "div")) == "bold text"

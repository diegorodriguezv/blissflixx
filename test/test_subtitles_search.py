"""
Turning a release name into a subtitle search.

The crash this guards against: a dated episode, a PPV special or a "3of3" part
is classified as an episode without a season, and the search read
`info["season"]` anyway. Subtitle search answered with a KeyError traceback.
"""

import pytest

from lib.api.subtitles import _search_name


class TestEpisodesWithASeason:
    def test_a_normal_episode_keeps_its_numbers(self):
        found = _search_name("The.Pacific.S01E09.1080p.BluRay.x264")
        assert found["kind"] == "episode"
        assert found["title"]
        assert found["season"] == 1
        assert found["episode"] == 9


class TestEpisodesWithoutASeason:
    @pytest.mark.parametrize(
        "name",
        [
            "The Daily Show 2026 11 13 1080p WEB h264",
            "Planet Earth 3of3 1080p BluRay",
            "UFC 300 PPV 1080p",
        ],
    )
    def test_it_does_not_crash(self, name):
        found = _search_name(name)
        assert found["kind"] == "episode"
        assert found["title"]

    def test_a_dated_episode_has_no_season_to_claim(self):
        found = _search_name("The Daily Show 2026 11 13 1080p WEB h264")
        assert "season" not in found
        assert "episode" not in found

    def test_a_part_keeps_its_number_but_not_a_season(self):
        found = _search_name("Planet Earth 3of3 1080p BluRay")
        assert found["episode"] == 3
        assert "season" not in found


class TestOtherKinds:
    def test_a_movie_keeps_its_year(self):
        found = _search_name("The Odyssey (2026) [1080p] [BluRay] [5.1]")
        assert found["kind"] == "movie"
        assert found["title"]
        assert found.get("year") == 2026

    def test_a_season_pack_is_a_season_search(self):
        found = _search_name("The Pacific Season 1 Complete 1080p BluRay")
        assert found["kind"] in ("tv_season", "tv_pack")
        assert found["title"]

    def test_a_series_pack_is_not_one_search(self):
        found = _search_name("Star Trek The Next Generation Complete Series 1-7")
        assert found["pack"] is True


class TestWhatIsSentToOpenSubtitles:
    """
    The command is the search. An episode is identified by its series and its
    numbers, a film by its title and year, and an IMDb id wins over both when
    the row already has one.
    """

    def _command(self, monkeypatch, **kwargs):
        from lib.api import subtitles as api

        calls = []

        def fake_check_output(cmd, **opts):
            calls.append(cmd)
            return b"[]"

        monkeypatch.setattr(api.subprocess, "check_output", fake_check_output)
        api.search(**kwargs)
        return calls[0]

    def test_an_episode_sends_season_and_episode_not_a_year(self, monkeypatch):
        cmd = self._command(
            monkeypatch, title="The Pacific", year=2010, season=1, episode=9
        )
        assert cmd[cmd.index("-t") + 1] == "The Pacific"
        assert cmd[cmd.index("-s") + 1] == "1"
        assert cmd[cmd.index("-e") + 1] == "9"
        assert "-y" not in cmd

    def test_a_movie_sends_its_year(self, monkeypatch):
        cmd = self._command(monkeypatch, title="The Odyssey", year=2026)
        assert cmd[cmd.index("-y") + 1] == "2026"
        assert "-s" not in cmd

    def test_a_known_imdb_id_travels_with_the_search(self, monkeypatch):
        cmd = self._command(
            monkeypatch,
            title="The Odyssey",
            year=2026,
            imdb="tt1234567",
        )
        assert cmd[cmd.index("-i") + 1] == "tt1234567"

    def test_a_file_with_context_is_searched_by_the_context(self, monkeypatch):
        # The file says "Episode 09" and nothing else; the context says what
        # series it belongs to. Without it the search is for the file name.
        cmd = self._command(
            monkeypatch,
            name="The Pacific Episode 09.mp4",
            context={
                "series": "The Pacific",
                "year": 2010,
                "season": 1,
                "episode": 9,
            },
        )
        assert cmd[cmd.index("-t") + 1] == "The Pacific"
        assert cmd[cmd.index("-s") + 1] == "1"
        assert cmd[cmd.index("-e") + 1] == "9"

    def test_a_whole_series_pack_is_not_one_search(self, monkeypatch):
        from lib.api import subtitles as api

        def fail(cmd, **opts):
            raise AssertionError("a series pack must not reach OpenSubtitles")

        monkeypatch.setattr(api.subprocess, "check_output", fail)
        answer = api.search(name="Star Trek The Next Generation Complete Series 1-7")
        assert answer["pack"] is True
        assert answer["results"] == []

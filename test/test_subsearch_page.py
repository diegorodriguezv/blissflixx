"""
The Add Subtitles page.

Two things went wrong here and produced one report: the tick boxes could not be
ticked, and the button under them sent an empty choice to a server that rejects
it, so pressing it ended in a 400.

Riot 2 calls preventDefault() for a click handler unless the handler returns
true. For a checkbox that default action is the tick itself, so the box was
ticked and reverted before the page's update ran: the model filled up behind a
box that never looked ticked.
"""

import pathlib
import re


def _page():
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "html" / "pages" / "subsearch.html").read_text()


def _method(source, name):
    """The body of a tag method, up to the closing brace at its indent."""
    match = re.search(
        r"^    " + re.escape(name) + r"\([^)]*\) \{\n(.*?)^    \}",
        source,
        re.S | re.M,
    )
    assert match, name
    return match.group(1)


class TestTheTickBoxes:
    def test_a_click_keeps_the_tick(self):
        body = _method(_page(), "toggle")
        assert "return true" in body
        assert "data-idx" in body

    def test_the_clicked_box_is_the_one_recorded(self):
        body = _method(_page(), "toggle")
        assert "e.target.checked" in body
        assert "filter" in body


class TestTheButton:
    def test_it_is_disabled_until_something_is_ticked(self):
        page = _page()
        assert "disabled={ !chosen.length }" in page
        # The empty array is truthy, so show= never hid it.
        assert "show={ chosen }" not in page

    def test_the_page_does_not_send_an_empty_choice(self):
        body = _method(_page(), "play")
        assert "if (!self.chosen.length) return" in body
        assert body.index("if (!self.chosen.length) return") < body.index("add_subtitles")


class TestTheSearchTarget:
    """
    The search is for the current file, but neither its name nor the pack's is
    enough on its own. The row's context fills the gaps, and all of it stays
    editable.
    """

    def test_the_page_has_season_and_episode_fields(self):
        page = _page()
        assert 'name="f_season"' in page
        assert 'name="f_episode"' in page

    def test_a_context_fills_the_fields(self):
        page = _page()
        assert "self.context.series" in page
        assert "self.context.title" in page
        assert "self.context.season" in page
        assert "self.context.episode" in page
        assert "self.context.imdb" in page

    def test_the_release_name_is_kept_for_the_server(self):
        body = _method(_page(), "search")
        assert "name: self.name" in body
        assert "title: title || null" in body
        assert "season:" in body
        assert "episode:" in body
        assert "imdb: self.imdb" in body

    def test_the_server_answer_fills_what_is_empty(self):
        page = _page()
        assert "data.season && !self.f_season.value" in page
        assert "data.episode && !self.f_episode.value" in page
        assert "data.imdb" in page


class TestTheContextIsPassed:
    def test_a_row_hands_its_context_to_the_search(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "itemlist.html").read_text()
        assert "subtitleContext" in tag
        assert "JSON.stringify(context)" in tag

    def test_the_files_page_names_the_pack(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "torrfiles.html").read_text()
        assert "title:self.title" in page


class TestWaitingForResults:
    """
    A search can take a while, and the page sat still until it answered. The
    note that says what is happening needs something moving beside it, or it
    reads as a result that has already arrived.
    """

    def test_the_note_carries_a_spinner(self):
        page = _page()
        match = re.search(
            r'<span show=\{ searching \} class="search-note">(.*?)</span>',
            page,
            re.S,
        )
        assert match, "the searching note is gone"
        assert "fa-spinner" in match.group(1)
        assert "fa-spin" in match.group(1)

    def test_the_spinner_comes_before_the_words(self):
        page = _page()
        assert page.index("fa-spinner") < page.index("Searching OpenSubtitles...")

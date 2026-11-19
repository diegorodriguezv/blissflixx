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

    def test_the_context_goes_to_the_server_rather_than_the_fields(self):
        # Prefilling the title from the context hid the release name from the
        # server: a channel row whose context was the whole name was searched
        # as a title, and "1of3" never became an episode.
        body = _method(_page(), "search")
        assert "context: self.context" in body
        page = _page()
        # The context may be shown ("Title: ..."), but it must not become the
        # search fields: those start empty so the server reads the name.
        assert "self.f_title.value = self.context" not in page
        assert "self.f_year.value = self.context" not in page
        assert "self.f_season.value = self.context" not in page
        assert "self.f_episode.value = self.context" not in page

    def test_the_imdb_id_the_row_knew_still_travels(self):
        body = _method(_page(), "search")
        assert "imdb: self.imdb" in body

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


class TestTheFileNameIsVisible:
    def test_the_page_says_which_file_the_search_is_for(self):
        page = _page()
        assert 'class="subs-file"' in page
        assert "File:" in page

    def test_an_exactly_named_result_is_highlighted(self):
        page = _page()
        assert "sub.exact ? 'exact'" in page
        assert "subs-exact-tag" in page

    def test_the_answer_carries_the_mark(self):
        assert "exact: !!r.exact" in _page()


class TestTheTitleAndFolder:
    def test_the_page_shows_the_file_the_title_and_the_folder(self):
        page = _page()
        for label in ("File:", "Title:", "Folder:"):
            assert label in page
        assert "{ searchTitle }" in page
        assert "{ folder }" in page

    def test_the_answer_marks_title_and_folder_matches(self):
        page = _page()
        assert "!!r.matches_title" in page
        assert "!!r.matches_folder" in page
        assert "sub.folder ? 'folder'" in page
        assert "sub.title ? 'title'" in page

    def test_each_kind_of_match_says_so(self):
        page = _page()
        assert "same name" in page
        assert "in folder" in page
        assert 'class="subs-exact-tag title"' in page

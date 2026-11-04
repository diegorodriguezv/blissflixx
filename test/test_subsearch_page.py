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

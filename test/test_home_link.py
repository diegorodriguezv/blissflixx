"""The way home, and the fact that there is not a second way.

The channels are the main page. There was a Channels page -- identical to the
main page, reachable by its own hash and advertised by an icon in the top bar --
and the icon was the only reason anyone ever went there, since the main page is
where the empty hash lands anyway.

Nothing pinned either link, which is why both survived every other change to the
interface. These do.
"""

import pathlib
import re

import pytest

from lib import locations

TOPNAV = "tags/topnav.html"
ERROR = "pages/error.html"


def markup(name):
    with open(pathlib.Path(locations.HTML_PATH) / name) as handle:
        return handle.read()


def code(name):
    """The file with its comments out.

    The comment above the name in the top bar quotes href="" and href="#" while
    explaining why the second one is there, and a grep that does not strip
    comments finds the explanation instead of the anchor. Which is how this file
    managed to fail its own test.

    The stylesheet has the same problem in its own syntax, so it gets its own
    comment syntax stripped.
    """
    source = markup(name)
    pattern = r"/\*.*?\*/" if name.endswith(".css") else r"<!--.*?-->"
    return re.sub(pattern, "", source, flags=re.S)


class TestTheNameGoesHome:
    def test_the_top_left_is_a_link(self):
        source = code(TOPNAV)
        brand = re.search(
            r"<li>\s*(<!--.*?-->\s*)?<a [^>]*>.*?</a>\s*</li>", source, re.S
        )
        assert brand is not None, "the top-left is not a link"
        assert 'href="#"' in brand.group(0), brand.group(0)

    def test_and_not_an_empty_one(self):
        """
        href="" resolves to the current document, so clicking it does nothing at
        all -- the same bug the channel rows in the manager had. "#" clears the
        fragment, which the router hears, and it is not a reload: a full reload
        would drop whatever is playing.
        """
        source = code(TOPNAV)
        assert 'href=""' not in source, "an empty href in the top bar does nothing"
        assert 'href="#"' in source, "the way home is not there"

    def test_and_the_router_lands_on_the_channels(self):
        """
        Where "#" actually goes. The empty fragment is what the main page is,
        because app.html defaults the route to the channels page.
        """
        app = code("app.html")
        assert "page = page || 'channels'" in app, app


class TestTheNameDoesNotInvertOnHover:
    """
    It is a link, and it looks like one that is not being hovered.

    Every anchor on this interface is white at rest and stays white -- theme.css
    says so once for all of them -- so the only thing hovering a link in the top
    bar changes is that one rule putting a white block behind it. Behind a
    thirty-pixel word that reads as something broken rather than as an
    affordance, so the rule is scoped to the links on the right.

    Scoped rather than given a rule of its own, because the left-hand list holds
    nothing else.
    """

    def _hover_rule(self):
        css = code("css/theme.css")
        found = re.search(r"^(\.topnav [^{]*:hover)\s*\{([^}]*)\}", css, re.M)
        assert found is not None, "the top bar has no hover rule for its links"
        return found.group(1), found.group(2)

    def test_the_hover_block_is_not_behind_the_name(self):
        selector, _ = self._hover_rule()
        # A :hover selector only ever matches while the pointer is on the
        # element, so this asks the question the stylesheet can answer: which
        # elements can the rule reach at all?
        without_hover = selector.replace(":hover", "")
        assert "navitems-left" not in without_hover, (
            "the hover block is still behind the name: %s" % selector
        )

    def test_and_the_links_on_the_right_still_get_it(self):
        """
        Otherwise the icons lose the one thing that marks them as clickable,
        which is the reason the rule is there.
        """
        selector, _ = self._hover_rule()
        assert "navitems-right" in selector, selector

    def test_and_it_is_still_a_white_block_on_a_dark_colour(self):
        _, body = self._hover_rule()
        assert "background-color: #FFFFFF" in body, body
        assert "color: #2A435A" in body, body

    def test_and_the_left_list_holds_nothing_else(self):
        """
        The scoping is only safe while that is true, which is the sort of thing
        that stops being true quietly.
        """
        source = code(TOPNAV)
        left = re.search(r'<ul class="navitems navitems-left">(.*?)</ul>', source, re.S)
        assert left is not None, "no left-hand list in the top bar"
        anchors = re.findall(r"<a\b", left.group(1))
        assert len(anchors) == 1, (
            "the left-hand list holds %d links and only the name is meant to be "
            "exempt from the hover block" % len(anchors)
        )


class TestTheNameIsAlwaysThere:
    """
    The name is the name at every width.

    It used to carry Bootstrap's hidden-xs and hidden-sm, so below 992px of
    viewport the word went away and only the film icon was left. That is what a
    narrow window, a phone, or a browser zoomed past 125% all looked like, and
    it read as the site mark having gone missing rather than as a layout --
    there is nothing on screen saying the name is there on a wider screen.

    The size is deliberately untouched. The fix is to stop hiding a thing, not
    to shrink it until it fits; a roomier bar was never the problem.
    """

    def _brand(self):
        source = code(TOPNAV)
        found = re.search(r"<li>\s*<a [^>]*>(.*?)</a>\s*</li>", source, re.S)
        assert found is not None, "the top-left is not a link"
        return found.group(1)

    @pytest.mark.parametrize(
        "cls", ["hidden-xs", "hidden-sm", "hidden-md", "hidden-lg"]
    )
    def test_the_name_is_not_hidden_at_any_width(self, cls):
        brand = self._brand()
        assert cls not in brand, brand

    def test_and_it_is_the_same_word_at_the_same_size(self):
        """
        No font-size of its own, so it keeps the 30px every anchor in the bar
        gets from layout.css. A rule here would be a second place to change it.
        """
        brand = self._brand()
        assert "BLISSFLIXX" in brand, brand
        assert "font-size" not in brand, brand


class TestThereIsNoChannelsPage:
    def test_the_top_bar_does_not_advertise_one(self):
        source = code(TOPNAV)
        assert "#channels" not in source, (
            "the top bar links to a channels route again; the channels are the "
            "main page and the name beside them goes there"
        )

    def test_and_no_icon_for_it(self):
        source = code(TOPNAV)
        assert 'title:"Channels"' not in source, source
        # fa-th was the channels icon, and it is still the playlists icon's
        # neighbour -- so check the pair, not the class on its own.
        assert (
            re.search(r'icon:"fa-th"[,\s]', source) is None
        ), "fa-th is the channels icon; fa-th-list is the playlists one"

    def test_the_route_still_works_though(self):
        """
        Deliberately. #channels loads the same page, the error page used to point
        at it, and anything already open on it keeps working. What is gone is
        the advertisement, not the address.
        """
        import os

        page = os.path.join(locations.HTML_PATH, "pages", "channels.html")
        assert os.path.exists(page), (
            "pages/channels.html is what '#channels' and the empty hash both "
            "load. Removing it makes every open tab on that route fail with an "
            "empty page and nothing saying why."
        )


class TestTheErrorPageGoesHomeToo:
    def test_try_again_is_not_a_channels_link(self):
        source = code(ERROR)
        assert 'href="#channels"' not in source, source
        assert re.search(r'class="underline" href="#"[^>]*>try again<', source), source

    def test_and_it_is_still_a_link_at_all(self):
        """
        The same reasoning as the name in the top bar, and it was already an
        anchor, so it only needed its target changing.
        """
        source = code(ERROR)
        assert ">try again</a>" in source, source

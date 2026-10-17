"""
A channel's website.

Every channel writes its address once, as a link in its description, and
nothing ever read it -- the description is not rendered anywhere in the
interface. So the channel manager's links were href="", which resolves to the
current document: every channel pointed at "/".
"""

import re

import pytest

from lib import locations
from lib.api.channels import Channel, InstalledChannels


def _describe(text):
    """A channel object with a given description, and nothing else."""
    channel = Channel.__new__(Channel)
    return channel._website(text)


def _tag(name):
    return locations.HTML_PATH + "/tags/" + name


def _page(name):
    return locations.HTML_PATH + "/pages/" + name


def _read(path):
    with open(path) as handle:
        return handle.read()


def api_info(chid):
    """What the server says about one channel."""
    from lib.api import channels as api_channels

    return api_channels.info(chid=chid)


def _markup(name="itemlist.html"):
    """The tag with its comments out.

    Comments first: this file explains the bugs, and the explanations quote
    them, so grepping the source finds the description of a fault where the
    fault used to be.
    """
    return re.sub(r"<!--.*?-->", "", _read(_tag(name)), flags=re.S)


class TestTheWebsiteIsReadOutOfTheDescription:
    def test_it_takes_the_one_link_that_is_there(self):
        assert (
            _describe(
                "EZTV Torrents Channel "
                "(<a target='_blank' href='https://eztvx.to'>https://eztvx.to</a>)."
            )
            == "https://eztvx.to"
        )

    def test_surrounding_prose_and_the_link_text_are_ignored(self):
        """
        The description is prose with a link in it, not a link. Reading the
        text between the tags would give "https://eztvx.to" by luck here and
        something else entirely for a description that does not repeat the URL.
        """
        assert (
            _describe(
                "All the latest videos from the subreddit "
                "(<a href='http://www.reddit.com/r/videos'>click here</a>)."
            )
            == "http://www.reddit.com/r/videos"
        )

    def test_a_description_with_no_link_has_no_website(self):
        """
        None rather than a guess. A channel with no link keeps its title as the
        only thing to click, which is better than a link to somewhere the
        description did not mean.
        """
        assert _describe("Just some words, no link at all.") is None

    def test_it_only_accepts_something_that_is_an_address(self):
        """
        A relative or javascript: href in a description is not where this
        channel comes from, and putting it in front of a person as the channel's
        website is worse than showing nothing.
        """
        for bad in (
            "<a href='/local/path'>x</a>",
            "<a href='javascript:alert(1)'>x</a>",
            "<a href='mailto:someone@example.com'>x</a>",
            "<a href='search://query'>x</a>",
        ):
            assert _describe(bad) is None, bad

    def test_it_takes_the_first_when_there_is_more_than_one(self):
        """
        The convention is one link, and the first is the channel itself; a
        second is a claim about somewhere else.
        """
        assert (
            _describe("<a href='https://the-channel.example'>a</a> and "
                      "<a href='https://somewhere-else.example'>b</a>")
            == "https://the-channel.example"
        )

    def test_empty_and_missing_are_handled(self):
        assert _describe("") is None
        assert _describe(None) is None


class TestEveryChannelHasOne:
    """
    The reason this is read out of the description rather than declared
    separately: a second declaration is seventeen chances to write one and
    forget the other.
    """

    def test_every_installed_channel_carries_its_website(self):
        channels = InstalledChannels().getAll()
        assert channels, "no channels found"
        without = [
            c.getId()
            for c in channels
            if not re.match(r"^https?://\S+$", c.getInfo()["link"] or "")
        ]
        assert without == [], "channels with no usable website: %s" % without

    def test_it_is_the_link_in_its_own_description(self):
        for channel in InstalledChannels().getAll():
            info = channel.getInfo()
            assert info["link"] in info["desc"], channel.getId()

    def test_it_is_not_in_the_url_key(self):
        """
        A media item's "url" is the thing to play, and the interface plays what
        it finds there. A channel is not playable: putting its website under
        that key would mean a click in the manager asking yt-dlp to play
        bbc.co.uk.
        """
        for channel in InstalledChannels().getAll():
            assert "url" not in channel.getInfo(), channel.getId()


class TestTheSourceButton:
    """
    Where the button itself comes from.

    Offered by the server rather than built by the manager, so the label, the
    order and the URL are decided in one place next to the link they use, and so
    a channel manager rendering anything else gets the same set.
    """

    def _info(self, chid):
        from lib.api import channels as api_channels

        return api_channels.info(chid=chid)

    def test_every_channel_offers_one(self):
        without = [
            c.getId()
            for c in InstalledChannels().getAll()
            if not any(a["type"] == "sourcechannel" for a in self._info(c.getId())["actions"])
        ]
        assert without == [], "channels with no Source button: %s" % without

    def test_it_is_afthe_disable_button(self):
        for channel in InstalledChannels().getAll():
            labels = [a["label"] for a in self._info(channel.getId())["actions"]]
            assert labels[-1] == "Source", (channel.getId(), labels)
            assert "Disable" in labels or "Enable" in labels, labels

    def test_it_carries_the_channels_website(self):
        for channel in InstalledChannels().getAll():
            info = self._info(channel.getId())
            source = [a for a in info["actions"] if a["type"] == "sourcechannel"][0]
            assert source["dest"] == info["link"], channel.getId()

    def test_the_destination_is_under_a_name_nothing_else_uses(self):
        """
        Inside itemlist's each={ actions } an expression takes the action's own
        properties first and the row's after, so a bare "url" finds the row's
        url for any action without one of its own -- which is every action but
        this one, and would have put a video's address on its Disable button.

        So the name has to be one that collides with neither a row nor another
        action, and "dest" is.
        """
        row_keys = set()
        for channel in InstalledChannels().getAll():
            row_keys.update(channel.getInfo().keys())
        for channel in InstalledChannels().getAll():
            for action in self._info(channel.getId())["actions"]:
                if action["type"] == "sourcechannel":
                    assert "dest" not in row_keys, "the row already carries dest"

    def test_an_enabled_and_a_disabled_channel_both_get_one(self):
        """
        It sits beside whichever button is there, so it cannot end up only on
        the channels that are on -- which is the shape that matters, because a
        disabled channel is the one a person is in here to look at.

        Turned off and on again within the test, which writes to the temporary
        settings the whole suite is redirected to.
        """
        from lib.api import channels as api_channels

        chid = InstalledChannels().getAll()[0].getId()
        try:
            on = [a["label"] for a in self._info(chid)["actions"]]
            api_channels.disable(chid=chid)
            off = [a["label"] for a in self._info(chid)["actions"]]
        finally:
            api_channels.enable(chid=chid)
        assert on[-1] == "Source" and on[0] == "Disable", on
        assert off[-1] == "Source" and off[0] == "Enable", off

    def test_the_channel_is_not_written_to_by_asking_for_it(self):
        """
        info() adds actions and settings to what it returns. Without a copy
        those land in the channel's own stored info, which list_enabled() also
        reads -- so asking for one channel's details gave every other listing a
        Disable button and a settings block nobody asked for.
        """
        from lib.api import channels as api_channels

        chid = InstalledChannels().getAll()[0].getId()
        api_channels.info(chid=chid)
        stored = api_channels.get_installed().getChannel(chid).getInfo()
        assert "actions" not in stored, "info() wrote actions into the channel"
        assert "settings" not in stored, "info() wrote settings into the channel"


class TestTheInterfaceUsesIt:
    def test_the_row_anchors_name_the_channel_not_the_website(self):
        """
        Clicking the icon or the title opens the channel, so that is what the
        link has to say. It said the channel's website, which is the Source
        button's job and is one button further along: a link naming somewhere
        the click does not go is worse than no link at all.

        An href="" would have been no better. That resolves to the current
        document, so every channel in the manager was a link to the page you
        were already on.
        """
        markup = _markup()
        row = re.findall(r"<a [^>]*onclick=\{ parent\.openItem \}[^>]*>", markup)
        assert len(row) == 2, "expected the icon and the title: %s" % row
        for anchor in row:
            assert "href={ route || '' }" in anchor, anchor

    def test_the_destination_comes_from_the_server(self):
        """
        The template does not work the route out for itself. It cannot: "route"
        being absent is how it knows a row is not a channel, and guessing that
        from the presence of a website would leave a channel without one
        pointing nowhere at all.
        """
        for channel in InstalledChannels().getAll():
            got = api_info(channel.getId())
            assert got["route"] == "#channel/" + channel.getId(), channel.getId()

    def test_it_is_spelled_the_way_the_channels_page_spells_it(self):
        """
        channels.html links a channel the same way. Two spellings of one route
        would drift apart the first time somebody changed one of them, and the
        only sign of it would be a link that goes somewhere nobody expected.
        """
        assert "#channel/{ id }" in _read(_page("channels.html"))

    def test_and_the_only_anchors_left_pointing_nowhere_are_the_buttons(self):
        """
        The action links -- Disable, Enable, Add To Playlist -- are controls
        and stay href="" on purpose: they run a function, and riot prevents the
        navigation. It is the anchors that open something that must name it.
        """
        source = _read(_tag("itemlist.html"))
        # Comments out first: this file explains the bug, and the explanation
        # quotes it.
        markup = re.sub(r"<!--.*?-->", "", source, flags=re.S)
        empty = [
            line.strip()
            for line in markup.splitlines()
            if 'href=""' in line and "item-action" not in line
        ]
        assert empty == [], "anchors that name nothing: %s" % empty

    def test_clicking_a_channel_opens_the_channel(self):
        """
        itemlist plays whatever url it is handed. A channel has no url, so a
        click in the manager used to throw a TypeError and do nothing visible;
        the manager now says what it means, which is open the channel.
        """
        source = _read(_page("chanman.html"))
        assert "openItem" in source, "the manager does not override openItem"
        assert "goRoute('channel'" in source, "it does not go to the channel"

    def test_the_source_button_is_handled(self):
        """
        An action the interface does not know is a click that does nothing.
        """
        source = _read(_tag("itemlist.html"))
        assert "case 'sourcechannel':" in source
        assert "action.dest" in source, "the source action does not use its URL"

    def test_it_opens_in_a_new_tab(self):
        """
        So the manager is still there afterwards. The href carries the same
        address, so middle-click and right-click already did this without the
        function at all.
        """
        source = _read(_tag("itemlist.html"))
        assert "window.open(action.dest, '_blank'" in source, (
            "the source button navigates in place rather than opening a tab"
        )


class TestTheHoverText:
    """
    What the browser shows when the pointer is over something.

    Firefox shows a status-bar line with the href; other browsers, and any
    television, show the title attribute and little else. Both are set to the
    same thing so there is never a moment where the two disagree and only one of
    them is on screen.
    """

    def test_the_icon_and_the_title_are_named_after_the_channel(self):
        """
        The name, not the address.

        On a television the browser's own line is never seen and the tooltip is
        all a person gets to read. "#channel/bfch_r_documentaries" tells them
        nothing they did not already know from the row they are pointing at;
        "Documentaries" tells them what is about to open. The address is still
        in the href, for the browser's line and for middle-click.

        The icon carries no text of its own, so this is the only label it has --
        which is the reason it is here rather than left to the browser's default,
        which would be the whole href again.
        """
        markup = _markup()
        anchors = re.findall(r"<a [^>]*onclick=\{ parent\.openItem \}[^>]*>", markup)
        assert len(anchors) == 2, "expected the icon and the title: %s" % anchors
        for anchor in anchors:
            assert "title={ title }" in anchor, anchor
            # The href is allowed to quote one -- it is the address. The hover
            # text is the only thing that must not.
            assert "title={ route" not in anchor, (
                "the hover text quotes an address: %s" % anchor
            )

    def test_the_source_button_still_names_where_it_goes(self):
        """
        The other half of the same rule, and the reason the two differ. Source
        goes somewhere else entirely -- off this machine, to a site that may not
        have a name worth reading -- so there the address is the useful thing to
        say, and the name would say nothing at all.
        """
        markup = _markup()
        action = re.search(r"<a [^>]*class=\"item-action\"[^>]*>", markup)
        assert action is not None, "no action button anchor"
        assert "title={ dest || label }" in action.group(0), action.group(0)

    def test_the_action_buttons_carry_it(self):
        markup = _markup()
        action = re.search(r"<a [^>]*class=\"item-action\"[^>]*>", markup)
        assert action is not None, "no action button anchor"
        anchor = action.group(0)
        assert "title={ dest || label }" in anchor, anchor
        assert "href={ dest || '' }" in anchor, anchor

    def test_no_hover_text_can_render_the_word_undefined(self):
        """
        A title attribute whose expression resolves to nothing is how a person
        ends up being shown the word "undefined". Exactly two shapes are safe,
        and both have to be one of these:

        - the row's own title, which is the text on screen and so cannot be
          missing. The icon carries no text of its own, which is the reason the
          hover text is there;
        - a fallback, for anything optional. Most actions have no destination at
          all, so theirs falls back to the label.

        Anything else is a variable's name, in a tooltip, where a person cannot
        tell it from a real word.

        Looked at expression by expression rather than anchor by anchor: an
        anchor carries a fallback in its href often enough to satisfy a looser
        check that never once looked at the hover text.
        """
        markup = _markup()
        expressions = re.findall(r"title=\{([^}]*)\}", markup)
        assert expressions, "no hover text found to check"
        for expression in expressions:
            assert expression.strip() == "title" or "||" in expression, (
                "hover text with nothing to fall back on: %s" % expression
            )

    def test_and_itemlist_survives_a_row_that_is_not_playable(self):
        source = _read(_tag("itemlist.html"))
        assert "!item || !item.url" in source, (
            "openItem still reaches into item.url without checking, which "
            "throws on any row that is not a media item"
        )
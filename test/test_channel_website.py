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


class TestTheInterfaceUsesIt:
    def _tag(self, name):
        return (locations.HTML_PATH + "/tags/" + name)

    def _page(self, name):
        return (locations.HTML_PATH + "/pages/" + name)

    def _read(self, path):
        with open(path) as handle:
            return handle.read()

    def test_the_row_anchor_uses_the_link(self):
        """
        href="" is the bug. It resolves to the current document, so every
        channel link was a link to "/" -- the page you were already on.
        """
        source = self._read(self._tag("itemlist.html"))
        anchors = re.findall(r"<a href=\{[^}]*\}", source)
        assert anchors, "no anchor takes its href from an expression"
        for anchor in anchors:
            assert "link" in anchor, anchor

    def test_and_the_only_anchors_left_pointing_nowhere_are_the_buttons(self):
        """
        The action links -- Disable, Enable, Add To Playlist -- are controls
        and stay href="" on purpose: they run a function, and riot prevents the
        navigation. It is the anchors that open something that must name it.
        """
        source = self._read(self._tag("itemlist.html"))
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
        source = self._read(self._page("chanman.html"))
        assert "openItem" in source, "the manager does not override openItem"
        assert "goRoute('channel'" in source, "it does not go to the channel"

    def test_and_itemlist_survives_a_row_that_is_not_playable(self):
        source = self._read(self._tag("itemlist.html"))
        assert "!item || !item.url" in source, (
            "openItem still reaches into item.url without checking, which "
            "throws on any row that is not a media item"
        )
"""
Per-channel settings.

The values are local to an installation: they live beside the channel's
disabled flag in data/settings/channels, never in the repository. The channel
declares what it can set; the platform supplies a default feed and a subtitle
language so a channel gets useful options without writing any code.
"""

import types

import pytest

from lib.api import channels as api_channels
from lib.api.channels import Channel, InstalledChannels
from lib.api.common import ApiError


FEEDS = [
    {"title": "Compilation"},
    {"title": "Cuckold"},
    {"title": "Amateur"},
]


def _channel(feeds=None, declared=None, subtitles=True, chid="bfch_test"):
    """A channel object with just what the settings code reads."""
    channel = Channel.__new__(Channel)
    channel.chid = chid
    channel.image = "/img/icons/test.png"
    channel.module = types.SimpleNamespace()
    channel.declared_config = declared or {}
    channel.supports_subtitles = subtitles
    channel.info = {
        "title": "Test channel",
        "feeds": feeds if feeds is not None else FEEDS,
    }
    channel.configurable = channel._is_configurable()
    return channel


def _installed(*channels):
    inst = InstalledChannels.__new__(InstalledChannels)
    inst.channels = list(channels)
    inst.settings = {}
    return inst


class TestTheSchema:
    def test_a_channel_with_several_feeds_offers_a_default_feed(self):
        schema = _channel().getConfigSchema()
        assert "default_feed" in schema
        values = [choice["value"] for choice in schema["default_feed"]["choices"]]
        assert "" in values  # "first feed"
        assert "Cuckold" in values

    def test_a_channel_with_one_feed_has_no_default_feed_to_choose(self):
        channel = _channel(feeds=[{"title": "Only"}])
        assert "default_feed" not in channel.getConfigSchema()

    def test_a_channel_that_supports_subtitles_offers_a_language(self):
        schema = _channel().getConfigSchema()
        assert schema["subtitles_lang"]["default"] == ""
        assert schema["subtitles"]["default"] is True
        # The language only means something while the button is on.
        assert schema["subtitles_lang"]["depends_on"] == "subtitles"
        codes = [choice["value"] for choice in schema["subtitles_lang"]["choices"]]
        assert "" in codes
        assert "eng" in codes

    def test_a_channel_without_subtitles_does_not_offer_a_language(self):
        schema = _channel(subtitles=False).getConfigSchema()
        assert "subtitles_lang" not in schema
        assert "subtitles" not in schema

    def test_a_channel_may_add_its_own_settings(self):
        declared = {
            "quality": {
                "label": "Quality",
                "type": "choice",
                "default": "any",
                "choices": [{"value": "any", "label": "Any"}],
            }
        }
        schema = _channel(declared=declared).getConfigSchema()
        assert schema["quality"] == declared["quality"]

    def test_the_full_feed_list_is_only_fetched_when_expanding(self):
        channel = _channel()
        channel.module = types.SimpleNamespace(
            feedlist_full=lambda: [{"title": "Brazil"}, {"title": "Cuckold"}]
        )
        assert "Brazil" not in [
            c["value"] for c in channel.getConfigSchema()["default_feed"]["choices"]
        ]
        expanded = channel.getConfigSchema(expand=True)
        assert "Brazil" in [c["value"] for c in expanded["default_feed"]["choices"]]


class TestTheValues:
    def test_defaults_apply_with_nothing_stored(self):
        values = _channel().getConfig()
        assert values["default_feed"] == ""
        assert values["subtitles_lang"] == ""

    def test_stored_values_win(self):
        values = _channel().getConfig({"default_feed": "Cuckold", "subtitles_lang": "spa"})
        assert values["default_feed"] == "Cuckold"
        assert values["subtitles_lang"] == "spa"

    def test_unknown_stored_keys_are_dropped(self):
        values = _channel().getConfig({"nonsense": "x"})
        assert "nonsense" not in values

    def test_the_default_feed_is_resolved_by_title(self):
        values = {"default_feed": "cuckold"}
        assert _channel().getDefaultFeedIndex(values) == 1

    def test_no_default_feed_means_the_first(self):
        assert _channel().getDefaultFeedIndex({}) == 0

    def test_an_unknown_default_feed_falls_back_to_the_first(self):
        assert _channel().getDefaultFeedIndex({"default_feed": "nope"}) == 0


class TestTheRows:
    def test_the_channel_language_overrides_the_global_one(self):
        from lib.playitem import PlayItem, PlayItemList

        channel = _channel()
        listing = PlayItemList()
        listing.add(PlayItem("A film", None, "http://x/film.mp4", subs={"lang": "eng"}))
        rows = channel.getPlayItems(listing, {"subtitles_lang": "spa"})
        assert rows[0]["subs"]["lang"] == "spa"

    def test_without_a_channel_language_the_row_keeps_its_own(self):
        from lib.playitem import PlayItem, PlayItemList

        channel = _channel()
        listing = PlayItemList()
        listing.add(PlayItem("A film", None, "http://x/film.mp4", subs={"lang": "eng"}))
        rows = channel.getPlayItems(listing, {"subtitles_lang": ""})
        assert rows[0]["subs"]["lang"] == "eng"

    def test_add_subtitles_is_dropped_when_the_channel_does_not_support_them(self):
        from lib.playitem import PlayItem, PlayItemList

        listing = PlayItemList()
        listing.add(PlayItem("A film", None, "http://x/film.mp4"))
        rows = _channel(subtitles=False).getPlayItems(listing)
        types_offered = [a["type"] for a in rows[0]["actions"]]
        assert "addsubtitles" not in types_offered
        assert "addplaylist" in types_offered

    def test_add_subtitles_stays_when_the_channel_supports_them(self):
        from lib.playitem import PlayItem, PlayItemList

        listing = PlayItemList()
        listing.add(PlayItem("A film", None, "http://x/film.mp4"))
        rows = _channel().getPlayItems(listing)
        assert "addsubtitles" in [a["type"] for a in rows[0]["actions"]]

    def test_add_subtitles_can_be_turned_off_for_this_installation(self):
        """
        A channel that technically supports subtitles may still have no use for
        them -- adult material, for one. The switch is local, so one install can
        differ from another without changing the channel.
        """
        from lib.playitem import PlayItem, PlayItemList

        listing = PlayItemList()
        listing.add(PlayItem("A film", None, "http://x/film.mp4"))
        rows = _channel().getPlayItems(listing, {"subtitles": False})
        assert "addsubtitles" not in [a["type"] for a in rows[0]["actions"]]

    def test_the_subtitles_switch_defaults_on(self):
        assert _channel().getConfig()["subtitles"] is True


class TestBlockedWords:
    """
    A local block list for rows whose title carries a word the box cannot use
    or does not want: a codec it cannot decode, a language nobody here speaks.
    """

    def _rows(self, *titles):
        from lib.playitem import PlayItem, PlayItemList

        listing = PlayItemList()
        for title in titles:
            listing.add(PlayItem(title, None, "http://x/" + title))
        return listing

    def test_the_schema_offers_a_block_list(self):
        schema = _channel().getConfigSchema()
        assert schema["block_words"]["type"] == "text"
        assert schema["block_words"]["default"] == ""

    def test_words_are_split_on_commas_and_newlines(self):
        assert api_channels.blocked_words("telugu, h265\nx265") == [
            "telugu",
            "h265",
            "x265",
        ]

    def test_a_phrase_keeps_its_spaces(self):
        assert api_channels.blocked_words("good day") == ["good day"]

    def test_an_empty_list_blocks_nothing(self):
        assert api_channels.blocked_words("") == []
        assert api_channels.blocked_words(None) == []

    def test_matching_ignores_case(self):
        assert api_channels.is_blocked("Film.TELUGU.1080p", ["telugu"])

    def test_rows_with_a_blocked_word_are_hidden(self):
        rows = _channel().getPlayItems(
            self._rows("Film 1080p", "Film 720p", "Film x265"),
            {"block_words": "1080p, x265"},
        )
        assert [row["title"] for row in rows] == ["Film 720p"]

    def test_a_blocked_phrase_matches_inside_the_title(self):
        rows = _channel().getPlayItems(
            self._rows("A Good Day to Die Hard", "Another Film"),
            {"block_words": "good day"},
        )
        assert [row["title"] for row in rows] == ["Another Film"]

    def test_an_empty_list_keeps_every_row(self):
        rows = _channel().getPlayItems(
            self._rows("Film 1080p", "Film 720p"), {"block_words": ""}
        )
        assert len(rows) == 2


class TestConfigurable:
    def test_subtitles_alone_make_a_channel_configurable(self):
        assert _channel(feeds=[{"title": "Only"}])._is_configurable()

    def test_a_single_feed_without_subtitles_is_not_configurable(self):
        assert not _channel(feeds=[{"title": "Only"}], subtitles=False)._is_configurable()

    def test_declared_settings_make_a_channel_configurable(self):
        channel = _channel(feeds=[{"title": "Only"}], subtitles=False, declared={"x": {}})
        assert channel._is_configurable()


class TestStoring:
    def test_a_value_round_trips_through_the_settings_file(self, settings):
        channel = _channel()
        inst = _installed(channel)
        inst.setChannelConfig("bfch_test", {"default_feed": "Cuckold"})
        assert inst.getChannelConfig("bfch_test")["default_feed"] == "Cuckold"
        settings._cache.clear()
        stored = settings.load("channels")
        assert stored["bfch_test"]["config"]["default_feed"] == "Cuckold"

    def test_storing_leaves_the_disabled_flag_alone(self, settings):
        channel = _channel()
        inst = _installed(channel)
        inst.settings = {"bfch_test": {"disabled": True}}
        inst.setChannelConfig("bfch_test", {"subtitles_lang": "spa"})
        assert inst.settings["bfch_test"]["disabled"] is True
        assert inst.settings["bfch_test"]["config"]["subtitles_lang"] == "spa"

    def test_an_unknown_key_is_refused(self, settings):
        inst = _installed(_channel())
        with pytest.raises(ApiError):
            inst.setChannelConfig("bfch_test", {"nonsense": "x"})

    def test_a_value_outside_the_choices_is_refused(self, settings):
        inst = _installed(_channel())
        with pytest.raises(ApiError):
            inst.setChannelConfig("bfch_test", {"default_feed": "nope"})

    def test_the_api_returns_the_schema_and_values(self, settings, monkeypatch):
        inst = _installed(_channel())
        monkeypatch.setattr(api_channels, "get_installed", lambda: inst)
        answer = api_channels.config(chid="bfch_test")
        assert answer["title"] is not None
        assert any(row["key"] == "default_feed" for row in answer["schema"])
        assert answer["values"]["default_feed"] == ""

    def test_the_api_saves_and_returns_the_new_values(self, settings, monkeypatch):
        inst = _installed(_channel())
        monkeypatch.setattr(api_channels, "get_installed", lambda: inst)
        answer = api_channels.set_config(
            chid="bfch_test", values={"default_feed": "Cuckold"}
        )
        assert answer["values"]["default_feed"] == "Cuckold"


class TestTheInterfaceKnows:
    def test_a_channel_row_opens_the_settings_page(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "itemlist.html").read_text()
        assert "channelsettings" in tag
        assert "Utils.goRoute('chansettings', item.id)" in tag

    def test_the_manage_page_does_not_carry_the_form(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "chanman.html").read_text()
        assert "<channelsettings" not in page

    def test_the_form_tag_renders_the_fields_it_is_given(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "channelsettings.html").read_text()
        assert "getConfig" in tag
        assert "setConfig" in tag
        assert "field.type == 'choice'" in tag

    def test_the_form_tag_is_loaded(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "index.html").read_text()
        assert "tags/channelsettings.html" in page

    def test_the_language_is_hidden_while_the_button_is_off(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "channelsettings.html").read_text()
        assert "field in shown" in tag
        assert "refreshShown" in tag
        assert "field.depends_on" in tag

    def test_typing_is_captured_without_leaving_the_field(self):
        """
        A value typed and submitted without blurring never fired `change`, so
        the form sent the value it loaded with -- an empty block list. The text
        field now records each keystroke, and Save reads the controls again.
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        tag = (root / "html" / "tags" / "channelsettings.html").read_text()
        assert "oninput={ typed }" in tag
        assert "self.collect()" in tag

    def test_the_global_language_has_a_home_on_the_settings_page(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "settings.html").read_text()
        assert "setSubtitleLang" in page
        assert "subtitleLanguages" in page

    def test_a_search_starts_from_the_global_language(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "subsearch.html").read_text()
        assert "self.lang = data.lang" in page

    def test_the_settings_page_is_the_form(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "chansettings.html").read_text()
        assert "<channelsettings" in page

    def test_a_channel_opens_its_default_feed(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        page = (root / "html" / "pages" / "channel.html").read_text()
        assert "defaultFeedIndex" in page
        assert "config.default_feed" in page or "config || {}).default_feed" in page


class TestTheGlobalLanguage:
    """The language a channel uses when it has not chosen one of its own."""

    def test_it_cannot_be_set_to_something_unknown(self, settings):
        from lib.api import subtitles

        with pytest.raises(ApiError):
            subtitles.set_language("nope")

    def test_it_round_trips(self, settings):
        from lib.api import subtitles

        subtitles.set_language("spa")
        settings._cache.clear()
        assert settings.load("subtitles") == {"lang": "spa"}
        assert subtitles.language()["lang"] == "spa"

    def test_it_exposes_the_choices_the_form_needs(self, settings):
        from lib.api import subtitles

        answer = subtitles.language()
        codes = [choice["code"] for choice in answer["languages"]]
        assert "eng" in codes and "spa" in codes

    def test_a_stored_value_that_is_gone_falls_back_to_english(self, settings):
        from lib.api import subtitles

        settings.save("subtitles", {"lang": "zzz"})
        settings._cache.clear()
        assert subtitles.language()["lang"] == "eng"

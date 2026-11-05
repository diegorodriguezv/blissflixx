"""
The Twitch channel's feeds.

The list is a fixed snapshot of the games and categories people watch most,
not a live "top games" call: feedlist() runs while the installed-channel list
is being built, and that must not wait on Twitch or depend on a token. The
streams inside a feed still come from Twitch.
"""

import bfch_twitch


class TestTheList:
    def test_it_is_a_snapshot_rather_than_a_live_call(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("feedlist() must not touch the network")

        monkeypatch.setattr(bfch_twitch, "get_json", refuse)
        feeds = bfch_twitch.feedlist()
        assert len(feeds) == 60

    def test_the_first_twelve_keep_their_places(self):
        """
        A route saved before the list grew still opens the same feed.
        """
        titles = [feed["title"] for feed in bfch_twitch.feedlist()[:12]]
        assert titles == [
            "Just Chatting",
            "League of Legends",
            "Fortnite",
            "Grand Theft Auto V",
            "Valorant",
            "Counter-Strike",
            "Dota 2",
            "Minecraft",
            "World of Warcraft",
            "Overwatch",
            "Apex Legends",
            "Call of Duty: Warzone",
        ]

    def test_no_game_appears_twice(self):
        feeds = bfch_twitch.feedlist()
        ids = [feed["id"] for feed in feeds]
        names = [feed["title"].lower() for feed in feeds]
        assert len(set(ids)) == len(ids)
        assert len(set(names)) == len(names)

    def test_every_feed_can_be_looked_up(self):
        for feed in bfch_twitch.feedlist():
            assert feed["title"].strip() == feed["title"]
            assert feed["id"].isdigit()

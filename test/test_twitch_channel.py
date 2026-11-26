"""
The Twitch channel's feeds.

The list is a fixed snapshot of popular games/categories, not a live "top games"
call: feedlist() runs while the installed-channel list is being built, and that
must not wait on Twitch or depend on a token. The streams inside a feed still
come from Twitch.
"""

import blissflixx.channels.bfch_twitch as bfch_twitch


class TestTheList:
    def test_it_is_a_snapshot_rather_than_a_live_call(self, monkeypatch):
        def refuse(*args, **kwargs):
            raise AssertionError("feedlist() must not touch the network")

        monkeypatch.setattr(bfch_twitch, "get_json", refuse)
        feeds = bfch_twitch.feedlist()
        assert len(feeds) >= 60

    def test_requested_games_and_default(self):
        feeds = bfch_twitch.feedlist()
        assert feeds[0]["title"] == "StarCraft II"
        assert feeds[0]["id"] == "490422"
        titles = [f["title"] for f in feeds]
        assert "Warcraft III" in titles
        assert "World of Warcraft" in titles

    def test_feeds_have_stable_names(self):
        feeds = bfch_twitch.feedlist()
        names = [f.get("name") for f in feeds]
        assert all(isinstance(n, str) and n for n in names)
        assert len(set(names)) == len(names)

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
            assert isinstance(feed.get("name", ""), str)


class TestFeedResolution:
    def test_feed_by_name_uses_stable_name_not_index(self, monkeypatch):
        calls = []

        def fake_get_json(url, params, headers=None):
            calls.append(params.get("game_id"))
            return {"data": []}

        monkeypatch.setattr(bfch_twitch, "get_json", fake_get_json)
        # Even if idx is wrong, name should resolve to Warcraft III.
        bfch_twitch.feed_by_name(0, "warcraft-iii")
        assert calls == ["12924"]

    def test_feed_out_of_range_defaults_to_first(self, monkeypatch):
        calls = []

        def fake_get_json(url, params, headers=None):
            calls.append(params.get("game_id"))
            return {"data": []}

        monkeypatch.setattr(bfch_twitch, "get_json", fake_get_json)
        bfch_twitch.feed(9999)
        assert calls == [bfch_twitch.feedlist()[0]["id"]]

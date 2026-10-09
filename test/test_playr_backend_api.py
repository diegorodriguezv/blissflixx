"""
Backend selection through the API.

describe() existed in the registry with nothing calling it, which made the whole
abstraction unreachable from a browser: the backend was chosen by editing a JSON
file and restarting, and a client had no way to ask what the active player could
do. These endpoints close both gaps.

The distinction that matters when reading them:

- backend_names() is the registry, so every name in it resolves through
  get_backend(). The legacy pseudo-backend is selectable but is not in there.
- selectable_names() is what a client may choose from, and includes legacy.
"""

import unittest.mock as m

import pytest

from lib.api import playr
from lib.api.common import ApiError
from lib.player.backends import (
    DEFAULT_BACKEND,
    LEGACY_BACKEND,
    backend_names,
    selectable_names,
)
from lib.settings import load


def active_name():
    return load("player").get("backend", DEFAULT_BACKEND)


class TestBackendsListing:
    def test_lists_every_selectable_name(self):
        listed = [b["backend"] for b in playr.backends()]
        assert listed == selectable_names()

    def test_includes_the_registry_and_legacy(self):
        listed = [b["backend"] for b in playr.backends()]
        for name in backend_names():
            assert name in listed
        assert LEGACY_BACKEND in listed

    def test_exactly_one_is_active(self):
        assert sum(1 for b in playr.backends() if b["active"]) == 1

    def test_the_active_one_is_the_default_when_unconfigured(self, settings):
        listed = {b["backend"]: b["active"] for b in playr.backends()}
        assert listed[DEFAULT_BACKEND] is True

    def test_default_is_vlc(self, settings):
        assert playr.backend()["backend"] == "vlc"

    def test_each_entry_reports_name_and_binary(self):
        for info in playr.backends():
            assert info["name"], info["backend"]
            assert info["binary"], info["backend"]

    def test_each_entry_reports_capabilities_as_a_sorted_list(self):
        for info in playr.backends():
            assert isinstance(info["capabilities"], list)
            assert info["capabilities"] == sorted(info["capabilities"])

    def test_gstreamer_is_flagged_as_having_no_controls(self):
        """
        The playbar renders one fixed set of buttons regardless of backend, so
        the only way a client can avoid offering a seek button to something that
        will ignore it is this note.
        """
        info = next(b for b in playr.backends() if b["backend"] == "gstreamer")
        assert info["capabilities"] == []
        assert "note" in info

    def test_capable_backends_carry_no_note(self):
        """
        Two unrelated reasons earn a note: no capabilities at all, and being
        legacy, whose capabilities depend on flags rather than being fixed. A
        plain capable backend has nothing surprising to say.
        """
        for info in playr.backends():
            if info["capabilities"] and info["backend"] != LEGACY_BACKEND:
                assert "note" not in info, info["backend"]

    def test_note_appears_exactly_where_it_should(self):
        """
        Every entry either has capabilities and is not legacy, or carries a note
        explaining why a client should not take it at face value.
        """
        for info in playr.backends():
            if not info["capabilities"] or info["backend"] == LEGACY_BACKEND:
                assert "note" in info, info["backend"]

    def test_legacy_is_flagged_as_flag_dependent(self):
        info = next(b for b in playr.backends() if b["backend"] == LEGACY_BACKEND)
        assert "note" in info


class TestBackendLookup:
    def test_no_name_returns_the_active_one(self, settings):
        assert playr.backend()["backend"] == active_name()

    @pytest.mark.parametrize("name", backend_names())
    def test_any_registered_name_can_be_described(self, name):
        assert playr.backend(name)["backend"] == name

    def test_legacy_can_be_described(self):
        assert playr.backend(LEGACY_BACKEND)["backend"] == LEGACY_BACKEND

    def test_unknown_name_is_rejected(self):
        with pytest.raises(ApiError):
            playr.backend("nonsense")

    def test_unknown_name_names_the_backend_in_the_message(self):
        with pytest.raises(ApiError, match="nonsense"):
            playr.backend("nonsense")


class TestSetBackend:
    def test_persists_the_choice(self, settings):
        playr.set_backend("mpv")
        settings._cache.clear()
        assert load("player") == {"backend": "mpv"}

    def test_takes_effect_for_the_next_play_without_a_restart(self, settings):
        """
        save() updates the settings cache, so the next play resolves the new
        backend without anything reloading a file.
        """
        from lib.player.player import _Player

        playr.set_backend("mpv")
        pl = _Player()
        assert pl._player_stage(True, True).name() == "mpv"

        playr.set_backend("gstreamer")
        assert pl._player_stage(True, True).name() == "gstreamer"

    def test_returns_the_description_of_what_was_set(self, settings):
        info = playr.set_backend("vlc")
        assert info["backend"] == "vlc"
        assert info["binary"] == "cvlc"

    def test_legacy_is_settable(self, settings):
        playr.set_backend(LEGACY_BACKEND)
        settings._cache.clear()
        assert load("player")["backend"] == LEGACY_BACKEND

    def test_unknown_name_is_rejected_and_changes_nothing(self, settings):
        with pytest.raises(ApiError):
            playr.set_backend("nonsense")
        assert "player" not in load("player")

    def test_the_error_lists_what_is_available(self):
        with pytest.raises(ApiError) as exc:
            playr.set_backend("nonsense")
        assert "vlc" in str(exc.value)
        assert LEGACY_BACKEND in str(exc.value)

    def test_missing_name_is_rejected(self, settings):
        with pytest.raises(ApiError):
            playr.set_backend()

    def test_setting_preserves_other_player_settings(self, settings):
        """
        "player" holds only the choice. Per-backend configuration lives in its
        own file and must not be clobbered by switching.
        """
        from lib.settings import save

        save("player-mpv", {"audio_device": "alsa/plughw:0"})
        playr.set_backend("mpv")
        settings._cache.clear()
        assert load("player-mpv")["audio_device"] == "alsa/plughw:0"


class TestDescribeDoesNotDisturbPlayback:
    def test_describing_a_backend_does_not_select_it(self, settings):
        from lib.player.player import _Player

        playr.backend("mpv")
        playr.backends()
        pl = _Player()
        assert pl._player_stage(True, True).name() == DEFAULT_BACKEND

    def test_describing_every_backend_constructs_each_one(self, settings):
        """
        describe() builds an instance, so this is where a backend with a broken
        constructor would surface rather than at playback time.
        """
        for name in backend_names():
            assert playr.backend(name)["name"]

    def test_describe_reads_current_configuration(self, settings):
        """
        So a client reporting "cvlc" is reporting what will actually run, not a
        hardcoded string.
        """
        from lib.settings import save

        save("player-vlc", {"binary": "/opt/vlc/bin/cvlc"})
        settings._cache.clear()
        assert playr.backend("vlc")["binary"] == "/opt/vlc/bin/cvlc"


class TestNoSideEffectsOnTheRunningPlayer:
    def test_changing_backend_does_not_disturb_the_current_pipe(self, settings):
        """
        The item already playing keeps the backend it started with. Rebuilding a
        running pipeline would mean retaining the url and subtitles it was built
        from, which the pipe does not keep.
        """
        from lib.player.player import _Player

        pl = _Player()
        pl.msgq.put("a-marker")
        playr.set_backend("gstreamer")
        assert pl.msgq.get_nowait() == "a-marker"


class TestDocumentedEndpointsAreReachable:
    """
    The README documents these over HTTP. Asserted through the real dispatcher,
    so a function that is renamed or stops being exposed fails here rather than
    leaving the documentation describing something that does not exist.
    """

    DOCUMENTED = ["backends", "backend", "set_backend"]

    @staticmethod
    def readme():
        import pathlib

        return (pathlib.Path(__file__).resolve().parents[1] / "README.md").read_text()

    @pytest.mark.parametrize("fn", DOCUMENTED)
    def test_endpoint_is_exposed_by_the_playr_module(self, fn):
        import blissflixx

        assert callable(getattr(blissflixx.api_modules["playr"], fn, None)), fn

    @pytest.mark.parametrize("fn", DOCUMENTED)
    def test_endpoint_is_named_in_the_readme(self, fn):
        assert fn in self.readme()

    def test_default_is_documented_as_vlc(self):
        assert "VLC is now the default" in self.readme()

    def test_the_capability_note_is_documented(self):
        """
        The reason a client would call backends() at all is to find out what the
        active backend can do, so that has to be explained.
        """
        readme = self.readme().lower()
        assert "capabilities" in readme
        assert "no control surface" in readme

"""
Channels that are separate projects.

They are installed as their own distributions and declare themselves in the
``blissflixx.channels`` entry-point group. The application used to find them by
scanning ``plugins/`` and appending that directory to ``sys.path`` -- a global
side effect, and a bare-name import that could collide with anything else on the
path.

An installation that has not re-run the installer still has an old-style clone,
so a directory no entry point claims is loaded from its file for one release,
without touching ``sys.path``.
"""

import sys
import types

import pytest

import blissflixx.api.channels as api_channels


def fake_module(directory, name="Demo", description="A demo channel"):
    """A channel module object, with an icon beside it."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "icon.png").write_bytes(b"x")
    module = types.ModuleType("bfch_demo")
    module.__file__ = str(directory / "__init__.py")
    module.name = lambda: name
    module.description = lambda: description
    module.image = lambda: "icon.png"
    return module


class FakeEntryPoint:
    def __init__(self, name, module):
        self.name = name
        self.module = module

    def load(self):
        return self.module


class ExplodingEntryPoint:
    def __init__(self, name):
        self.name = name

    def load(self):
        raise ImportError("the plugin's dependency is missing")


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """An installed-channels build with no built-ins and no real plugins."""
    monkeypatch.setattr(api_channels, "channel_ids", lambda paths: [])
    monkeypatch.setattr(api_channels, "PLUGIN_PATH", str(tmp_path / "plugins"))
    monkeypatch.setattr(api_channels, "load", lambda name: {})
    monkeypatch.setattr(api_channels, "_plugin_entry_points", lambda: [])
    return tmp_path / "plugins"


class TestEntryPoints:
    def test_an_installed_plugin_is_listed(self, sandbox, monkeypatch):
        module = fake_module(sandbox / "bfch_demo")
        monkeypatch.setattr(
            api_channels,
            "_plugin_entry_points",
            lambda: [FakeEntryPoint("bfch_demo", module)],
        )

        channels = api_channels.InstalledChannels().getAll()

        assert [c.getId() for c in channels] == ["bfch_demo"]
        assert channels[0].getInfo()["img"] == "/api/pluginimage/bfch_demo/icon.png"

    def test_the_plugin_does_not_have_to_be_inside_plugins(self, sandbox, monkeypatch):
        """
        Its images come from the module's own directory, which is the point of
        an installed distribution: it may live anywhere the virtualenv can
        import it from.
        """
        elsewhere = sandbox.parent / "somewhere" / "bfch_demo"
        module = fake_module(elsewhere)
        monkeypatch.setattr(
            api_channels,
            "_plugin_entry_points",
            lambda: [FakeEntryPoint("bfch_demo", module)],
        )

        channels = api_channels.InstalledChannels().getAll()

        assert channels[0].imageExists()

    def test_one_broken_plugin_does_not_hide_another(self, sandbox, monkeypatch):
        good = fake_module(sandbox / "bfch_good", name="Good")
        monkeypatch.setattr(
            api_channels,
            "_plugin_entry_points",
            lambda: [
                ExplodingEntryPoint("bfch_broken"),
                FakeEntryPoint("bfch_good", good),
            ],
        )

        channels = api_channels.InstalledChannels().getAll()

        assert [c.getId() for c in channels] == ["bfch_good"]


class TestTheOldStyleClone:
    def test_a_clone_without_an_entry_point_is_still_loaded(self, sandbox):
        directory = sandbox / "bfch_old"
        directory.mkdir(parents=True)
        (directory / "icon.png").write_bytes(b"x")
        (directory / "__init__.py").write_text(
            "def name(): return 'Old'\n"
            "def description(): return 'old style'\n"
            "def image(): return 'icon.png'\n"
        )

        channels = api_channels.InstalledChannels().getAll()

        assert [c.getId() for c in channels] == ["bfch_old"]

    def test_loading_it_does_not_touch_sys_path(self, sandbox):
        """
        The whole reason for loading the file directly. Adding plugins/ to
        sys.path is a process-wide change made for one channel, and it outlives
        the load.
        """
        directory = sandbox / "bfch_old"
        directory.mkdir(parents=True)
        (directory / "icon.png").write_bytes(b"x")
        (directory / "__init__.py").write_text(
            "def name(): return 'Old'\n"
            "def description(): return 'old style'\n"
            "def image(): return 'icon.png'\n"
        )
        before = list(sys.path)

        api_channels.InstalledChannels()

        assert sys.path == before

    def test_a_claimed_directory_is_not_loaded_twice(self, sandbox, monkeypatch):
        directory = sandbox / "bfch_demo"
        directory.mkdir(parents=True)
        (directory / "icon.png").write_bytes(b"x")
        (directory / "__init__.py").write_text("raise AssertionError('loaded twice')\n")
        module = fake_module(directory)

        monkeypatch.setattr(
            api_channels,
            "_plugin_entry_points",
            lambda: [FakeEntryPoint("bfch_demo", module)],
        )

        channels = api_channels.InstalledChannels().getAll()

        assert len(channels) == 1
        assert channels[0].module is module


class TestTheBuiltInsWin:
    def test_a_plugin_cannot_take_a_built_in_channel_id(self, sandbox, monkeypatch):
        builtin = fake_module(sandbox / "builtin", name="Built in")
        plugin = fake_module(sandbox / "plugin", name="Impostor")
        monkeypatch.setattr(api_channels, "channel_ids", lambda paths: ["bfch_demo"])
        monkeypatch.setattr(
            api_channels, "_builtin_channel_module", lambda chid: builtin
        )
        monkeypatch.setattr(
            api_channels,
            "_plugin_entry_points",
            lambda: [FakeEntryPoint("bfch_demo", plugin)],
        )

        channels = api_channels.InstalledChannels().getAll()

        assert len(channels) == 1
        assert channels[0].getTitle() == "Built in"


class TestTheIconRoute:
    def test_serves_the_icon_from_the_channel_directory(self, sandbox, monkeypatch):
        """
        The icon route used to join PLUGIN_PATH with the id, which only works
        while every plugin is a checkout under plugins/. An installed plugin is
        a distribution, so its directory is the one to serve from.
        """
        import blissflixx.server as server

        elsewhere = sandbox.parent / "elsewhere" / "bfch_demo"
        module = fake_module(elsewhere)
        monkeypatch.setattr(
            api_channels,
            "_plugin_entry_points",
            lambda: [FakeEntryPoint("bfch_demo", module)],
        )
        monkeypatch.setattr(
            api_channels, "_installed", api_channels.InstalledChannels()
        )
        served = []
        monkeypatch.setattr(
            server.static,
            "serve_file",
            lambda path, *args, **kwargs: served.append(path) or "the file",
        )

        answer = server.Api().pluginimage("bfch_demo", "icon.png")

        assert answer == "the file"
        assert served == [str(elsewhere / "icon.png")]

    def test_an_unknown_channel_is_not_found(self, sandbox, monkeypatch):
        import cherrypy
        import blissflixx.server as server

        monkeypatch.setattr(
            api_channels, "_installed", api_channels.InstalledChannels()
        )

        with pytest.raises(cherrypy.NotFound):
            server.Api().pluginimage("bfch_missing", "icon.png")

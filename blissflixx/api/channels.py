import cherrypy
import glob
import importlib
import importlib.metadata
import importlib.util
import os
import pkgutil
import re
import shlex
import shutil
import sys
import traceback
from os import path
from queue import Queue
from threading import Lock, Thread

#: The one channel backend that reads from a third party, imported for the
#: failure it raises when that third party cannot answer. A dependency being
#: unavailable is not a bug in this server and must not be reported as one, so it
#: is translated here into an ApiError that says what happened in words the
#: interface can put on screen.
from ..chanutils.reddit import ArchiveBusy
from ..chanutils.languages import LANGUAGES
from ..locations import PLUGIN_PATH, ROOT_PATH
from ..settings import load, save
from .common import ApiError

CHANID_GLOB = "bfch_*"

#: The package the channels shipped with the application live in. Their ids are
#: the last component of the package name, which is also what the settings file
#: and every URL carry -- unchanged from when they were plain directories.
CHANNEL_PACKAGE = "blissflixx.channels"

#: Every channel name starts with this.
CHANNEL_PREFIX = "bfch_"

#: The entry-point group a channel distribution declares itself in. A plugin is
#: a package of its own -- cloned into plugins/, installed into the virtualenv --
#: and this is how the application finds it without knowing its directory.
CHANNEL_ENTRY_POINT_GROUP = "blissflixx.channels"


def channel_ids(package_paths):
    """
    The channel packages that exist under the given import paths.

    A directory with no ``__init__.py`` is not a package and is skipped. That
    matters after a channel is removed: git deletes the tracked files but leaves
    the directory when something untracked is still in it, and ``__pycache__``
    is exactly that. Python would import it as an implicit namespace package --
    an empty module that raises AttributeError on the first attribute read -- so
    every restart logged a traceback for a channel that had been deliberately
    deleted.
    """
    return sorted(
        name
        for _, name, is_pkg in pkgutil.iter_modules(package_paths)
        if is_pkg and name.startswith(CHANNEL_PREFIX)
    )


def _builtin_channel_module(chid):
    """Import one of the channels that ships with the application."""
    return importlib.import_module(CHANNEL_PACKAGE + "." + chid)


def _plugin_entry_points():
    """The channels installed as distributions of their own."""
    return importlib.metadata.entry_points().select(group=CHANNEL_ENTRY_POINT_GROUP)


def _load_legacy_plugin(directory, chid):
    """
    Import an old-style plugin directory without putting it on sys.path.

    Before entry points, plugins were cloned into plugins/ and imported by bare
    name, which meant appending that directory to sys.path: a global,
    process-wide side effect for one channel. Loading its file directly keeps an
    installation that has not re-run the installer working for one release
    without that. The module goes into sys.modules under its own name, so a
    second load in the same process reuses it the way an import would.
    """
    spec = importlib.util.spec_from_file_location(
        chid,
        path.join(directory, "__init__.py"),
        submodule_search_locations=[directory],
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[chid] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(chid, None)
        raise
    return module


#: A channel's own website, taken out of its description.
#:
#: Every channel puts exactly one link there -- the subreddit, the site it
#: scrapes -- and nothing ever read it. The description is not rendered anywhere
#: in the interface, so a channel's address was written once and then ignored.
#:
#: Read out of the description rather than asked for separately, because it is
#: already there and there are seventeen channels. A second declaration would
#: be seventeen chances to write one and forget the other.
#:
#: Only the first, because that is the convention: every description carries
#: one link, and a second would be a claim about somewhere else rather than the
#: channel itself.
_LINK_IN_DESC = re.compile(r"""href=['"]([^'"]+)['"]""")

#: What one of those looks like, so a channel description cannot quietly become
#: an anchor to somewhere unexpected.
_EXTERNAL_LINK = re.compile(r"^https?://\S+$")


def blocked_words(value):
    """
    The words in a block list, lowercased.

    Commas, semicolons, newlines and spaces all separate entries, so a list
    typed as "telugu h265 x265" is three words, which is what someone typing it
    expects. A phrase keeps its spaces when it is quoted: "good day" hides
    that title and leaves Training Day alone. Order and duplicates do not
    matter.
    """
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(part) for part in value]
    else:
        text = re.sub(r"[,;\n]+", " ", str(value))
        try:
            parts = shlex.split(text)
        except ValueError:
            # An unbalanced quote must not lose the rest of the list.
            parts = text.split()
    return [part.strip().casefold() for part in parts if part.strip()]


def is_blocked(title, words):
    """Whether a row's title carries any of the blocked words."""
    text = (title or "").casefold()
    return any(word in text for word in words)


def blocked_report(titles, words):
    """
    What a block list hides from one listing, word by word.

    A row can carry several words, so the per-word counts can add up to more
    than the rows hidden: `hidden` counts each row once, `words` counts it
    under every word it carries. Words keep the order they were typed in, and
    duplicates are counted once.
    """
    if not words:
        return None
    unique = []
    for word in words:
        word = str(word).casefold()
        if word not in unique:
            unique.append(word)
    counts = {word: 0 for word in unique}
    hidden = 0
    for title in titles:
        matched = [word for word in unique if is_blocked(title, [word])]
        if not matched:
            continue
        hidden += 1
        for word in matched:
            counts[word] += 1
    return {
        "rows": len(titles),
        "hidden": hidden,
        "words": [{"word": word, "count": counts[word]} for word in unique],
    }


class Channel:
    def __init__(self, chid, module, plugin):
        self.chid = chid
        self.module = module
        #: Where the channel lives: its images sit beside its code. Taken from
        #: the module, so a built-in channel, an installed plugin and a legacy
        #: plugin under plugins/ all work without a directory being passed in.
        self.cpath = path.dirname(path.abspath(module.__file__))
        name = module.name()
        desc = module.description()
        image = chid + "/" + module.image()
        if plugin:
            self.image = "/api/pluginimage/" + image
        else:
            self.image = "/api/chanimage/" + image
        search = False
        if hasattr(module, "search"):
            search = True
        feeds = None
        if hasattr(module, "feedlist"):
            feeds = module.feedlist()
        self.info = {
            "title": name,
            "id": chid,
            "img": self.image,
            "search": search,
            # "desc", not "subtitle". A media item carries a subtitle -- one
            # line of detail under its title, "Score: 12, 3 comments" or "Size:
            # 1.2GB" -- and PlayItem.to_dict() serialises it under that name.
            # This is the channel's description, a different thing entirely,
            # and it was arriving under the same key. Nothing read it: the
            # interface renders channel.img, title, search, feeds and id, and
            # never the description. So the collision cost nothing so far, but
            # it would mislead anyone reading an API response holding both a
            # channel and a list of its items.
            "desc": desc,
            "feeds": feeds,
            # Whether the channel can build a larger list than the one it
            # starts with. Only IPTV does; the page asks for that list after
            # the feed has loaded, and never for a channel that has none.
            "full_feeds": hasattr(module, "feedlist_full"),
            # The channel's website. Carried as "link" and not as "url": a media
            # item's "url" is the thing to play, and the interface plays what it
            # finds there. A channel is not playable and putting its website in
            # that key would mean a click on a channel in the manager asking
            # yt-dlp to play bbc.co.uk.
            "link": self._website(desc),
        }
        #: Extra settings the channel declares, if any: a dict, or a callable
        #: returning one. Evaluated at import, so it must be pure -- a channel
        #: that fetched its catalogue here would hold up every page that lists
        #: channels. `Channel.getConfigSchema()` adds the built-ins on top.
        declared = getattr(module, "config", None)
        try:
            declared = declared() if callable(declared) else declared
        except Exception:
            declared = None
        self.declared_config = declared if isinstance(declared, dict) else {}
        #: Whether Add Subtitles makes sense for this channel's rows. A plugin
        #: that carries material no subtitle service can help with sets
        #: SUPPORTS_SUBTITLES = False.
        self.supports_subtitles = getattr(module, "SUPPORTS_SUBTITLES", True)
        #: Whether the channel has anything at all to configure. The interface
        #: shows a Settings button only when this is true.
        self.configurable = self._is_configurable()
        self.info["configurable"] = self.configurable

    def _is_configurable(self):
        """Whether there is anything on this channel to set."""
        return bool(
            self.declared_config
            or self.supports_subtitles
            or len(self.info["feeds"] or []) > 1
        )

    @staticmethod
    def _website(desc):
        """
        The address out of the description, or None if there is not one.

        Returns None rather than guessing: a channel with no link shows no link
        and keeps its title as the only thing to click, which is better than a
        link to somewhere the description did not mean.
        """
        found = _LINK_IN_DESC.search(desc or "")
        if found is None:
            return None
        link = found.group(1).strip()
        return link if _EXTERNAL_LINK.match(link) else None

    def getId(self):
        return self.chid

    def getInfo(self):
        return self.info

    def getTitle(self):
        return self.info["title"]

    def getFeeds(self):
        return self.info["feeds"]

    def getConfigSchema(self, expand=False):
        """
        The settings this channel offers, in the order they should be shown.

        Two are built in, so a channel gets useful options without writing
        anything: a default feed when it has more than one, and a subtitle
        language when Add Subtitles is offered. A channel may add its own by
        declaring `config()`; those are merged last.

        `expand` asks for the full feed list, which for IPTV means fetching the
        catalogue. Never set it while listing channels: only the settings page
        needs it, and only when it is opened.
        """
        schema = {}
        feeds = self.getFeeds() or []
        if len(feeds) > 1:
            choices = [f.get("title") for f in feeds if f.get("title")]
            if expand:
                full = getattr(self.module, "feedlist_full", None)
                if callable(full):
                    try:
                        choices = [
                            f.get("title")
                            for f in (full() or feeds)
                            if f.get("title")
                        ]
                    except Exception:
                        # The catalogue could not be reached; the feeds we
                        # already have are still a usable list to choose from.
                        pass
            schema["default_feed"] = {
                "label": "Default feed",
                "type": "choice",
                "default": "",
                "choices": [{"value": "", "label": "First feed"}]
                + [{"value": c, "label": c} for c in choices],
                "hint": "The feed opened when this channel is first shown.",
            }
        schema["block_words"] = {
            "label": "Blocked words",
            "type": "text",
            "default": "",
            "hint": "Hide rows whose title contains any of these words. "
            "Separate them with commas or spaces: telugu, h265, x265, 1080p. "
            'Put a phrase in quotes: "good day".',
        }
        if self.supports_subtitles:
            global_lang = (load("subtitles").get("lang") or "eng")
            global_name = next(
                (name for code, name in LANGUAGES if code == global_lang),
                global_lang,
            )
            schema["subtitles"] = {
                "label": "Add Subtitles button",
                "type": "bool",
                "default": True,
                "hint": "Show Add Subtitles on this channel's rows.",
            }
            schema["subtitles_lang"] = {
                "label": "Subtitle language",
                "type": "choice",
                "default": "",
                "choices": [
                    {
                        "value": "",
                        "label": "Use the global setting (%s)" % global_name,
                    }
                ]
                + [{"value": code, "label": name} for code, name in LANGUAGES],
                "hint": "Used when Add Subtitles is chosen from this channel.",
                # Only worth showing while the button itself is on.
                "depends_on": "subtitles",
            }
        for key, spec in self.declared_config.items():
            if isinstance(spec, dict):
                schema[key] = spec
        return schema

    def getConfig(self, stored=None):
        """Effective settings: the declared defaults, then what is on disk."""
        stored = stored if isinstance(stored, dict) else {}
        schema = self.getConfigSchema()
        values = {}
        for key, spec in schema.items():
            values[key] = spec.get("default", "")
        for key, value in stored.items():
            if key in schema:
                values[key] = value
        return values

    def getDefaultFeedIndex(self, config=None):
        """The feed to open when the route names none, by its title."""
        wanted = str((config or {}).get("default_feed") or "").strip()
        if not wanted:
            return 0
        for index, feed in enumerate(self.getFeeds() or []):
            if (feed.get("title") or "").casefold() == wanted.casefold():
                return index
        return 0

    def getPlayItems(self, items, config=None):
        return self._filtered(items, config)[0]

    def _filtered(self, items, config=None):
        """
        The rows a channel shows, and the account of what the block list hid.

        The account travels with the rows rather than being computed by a
        second request: a feed that is cached on the page must bring the count
        it was filtered with.
        """
        items = items.to_dict()
        config = config if isinstance(config, dict) else {}
        lang = config.get("subtitles_lang") or ""
        blocked = blocked_words(config.get("block_words"))
        # A channel that cannot use subtitles never offers the button; one that
        # can may still have it turned off for this installation.
        subtitles_on = self.supports_subtitles and config.get("subtitles", True)
        # fix any missing images
        # and Add channel id to showmore items and actions
        for i in items:
            if "url" in i and i["url"].startswith("showmore://"):
                i["chid"] = self.chid
            if ("img" not in i) or (i["img"] is None):
                i["img"] = self.image
            if lang and isinstance(i.get("subs"), dict):
                # The channel's own language wins over the global setting.
                i["subs"]["lang"] = lang
            if not subtitles_on and i.get("actions"):
                i["actions"] = [
                    action
                    for action in i["actions"]
                    if action.get("type") != "addsubtitles"
                ]
            if "actions" in i:
                for action in i["actions"]:
                    if action["type"] == "showmore":
                        action["chid"] = self.chid

        report = None
        if blocked:
            report = blocked_report(
                [i.get("title") for i in items], blocked
            )
            kept = [i for i in items if not is_blocked(i.get("title"), blocked)]
            if report["hidden"]:
                cherrypy.log(
                    "%s: blocked %d of %d rows (%s)"
                    % (
                        self.chid,
                        report["hidden"],
                        report["rows"],
                        ", ".join(
                            "%s %d" % (w["word"], w["count"])
                            for w in report["words"]
                        ),
                    )
                )
            items = kept

        return items, report

    def getFeed(self, idx, config=None):
        return self._filtered(self.module.feed(idx), config)

    def getFeedByName(self, idx, name, config=None):
        return self._filtered(self.module.feed_by_name(idx, name), config)

    def search(self, q, config=None):
        return self._filtered(self.module.search(q), config)

    def showmore(self, link, config=None):
        return self.getPlayItems(self.module.showmore(link), config)

    def imageExists(self):
        return path.isfile(path.join(self.cpath, self.module.image()))


class InstalledChannels:
    def __init__(self):
        self._backup_integrated_plugins("oldplugins")
        self._refresh()

    # On june 2019 some community created plugins were integrated into the
    # main branch of blissflixx. To avoid possible clashes these plugins,
    # when found, are moved to a backup folder so that blissflixx uses the
    # code in the main channels.
    def _backup_integrated_plugins(self, folder):
        plugins = [
            "bfch_eztv",
            "bfch_kickass_torrents",
            "bfch_local_media",
            "bfch_pirate_bay",
            "bfch_yts_torrents",
        ]
        backup_folder = path.join(ROOT_PATH, folder)
        for p in plugins:
            plugin = path.join(PLUGIN_PATH, p)
            if path.exists(plugin):
                if not path.exists(backup_folder):
                    os.mkdir(backup_folder)
                shutil.move(plugin, backup_folder)

    def _refresh(self):
        channels = []
        package = importlib.import_module(CHANNEL_PACKAGE)
        for chid in channel_ids(package.__path__):
            try:
                channels.append(Channel(chid, _builtin_channel_module(chid), False))
            except Exception:
                # A broken channel must not take the whole server down, and it
                # need not raise ImportError: a bad selector raises ValueError,
                # a changed API shape raises KeyError, and so on.
                print(f"Failed to load channel {chid}")
                traceback.print_exc()
        plugins = {}
        for entry_point in _plugin_entry_points():
            try:
                plugins[entry_point.name] = entry_point.load()
            except Exception:
                print(f"Failed to load plugin {entry_point.name}")
                traceback.print_exc()
        # An installation that has not re-run the installer still has the
        # old-style clone. Load it from its file rather than from sys.path, and
        # only when no entry point already claimed it.
        for directory in sorted(glob.glob(path.join(PLUGIN_PATH, CHANID_GLOB))):
            chid = path.basename(directory)
            if chid in plugins or not path.isfile(path.join(directory, "__init__.py")):
                continue
            try:
                plugins[chid] = _load_legacy_plugin(directory, chid)
            except Exception:
                print(f"Failed to load plugin {directory}")
                traceback.print_exc()
        for chid, module in plugins.items():
            if any(chan.chid == chid for chan in channels):
                # A plugin may not take a built-in channel's id.
                continue
            try:
                channels.append(Channel(chid, module, True))
            except Exception:
                print(f"Failed to load plugin {chid}")
                traceback.print_exc()
        # Ignore channels with no image
        channels = [chan for chan in channels if chan.imageExists()]
        self.channels = sorted(channels, key=lambda chan: chan.getTitle().upper())
        self.settings = load("channels")

    def _set_config(self, chid, key, value):
        settings = self.getChannelSettings(chid)
        settings[key] = value
        self.settings[chid] = settings
        self._save_config()

    def _save_config(self):
        save("channels", self.settings)

    def enableChannel(self, chid):
        self._set_config(chid, "disabled", False)

    def disableChannel(self, chid):
        self._set_config(chid, "disabled", True)

    def getEnabled(self):
        enabled = []
        for c in self.channels:
            if self.isEnabled(c.getId()):
                enabled.append(c)
        return enabled

    def getAll(self):
        return self.channels

    def getChannel(self, chid):
        for chan in self.channels:
            if chan.getId() == chid:
                return chan
        raise ApiError("Unknown channel ID: '" + chid + "'")

    def isEnabled(self, chid):
        settings = self.getChannelSettings(chid)
        if "disabled" in settings and settings["disabled"] == True:
            return False
        else:
            return True

    def getChannelSettings(self, chid):
        settings = {}
        if chid in self.settings:
            settings = self.settings[chid]
        return settings

    def getChannelConfig(self, chid):
        """The channel's effective options: defaults with the stored values."""
        channel = self.getChannel(chid)
        return channel.getConfig(self.getChannelSettings(chid).get("config"))

    def getChannelConfigSchema(self, chid, expand=False):
        return self.getChannel(chid).getConfigSchema(expand)

    def setChannelConfig(self, chid, values):
        """
        Store one channel's options, locally for this installation.

        Keys and choices are checked against the schema the channel declares, so
        a typo in a hand-written request is reported rather than stored and
        silently ignored. Values live under the channel's entry in
        data/settings/channels, beside its disabled flag.
        """
        if not isinstance(values, dict):
            raise ApiError("Channel settings must be a mapping")
        channel = self.getChannel(chid)
        schema = channel.getConfigSchema(expand=True)
        settings = self.getChannelSettings(chid)
        config = dict(settings.get("config") or {})
        for key, value in values.items():
            if key not in schema:
                raise ApiError(
                    "Unknown setting '%s' for channel '%s'" % (key, chid)
                )
            spec = schema[key]
            kind = spec.get("type", "text")
            if kind == "choice":
                choices = [
                    choice.get("value")
                    for choice in spec.get("choices", [])
                    if isinstance(choice, dict)
                ]
                if choices and value not in choices:
                    raise ApiError(
                        "Invalid value for setting '%s' on channel '%s'"
                        % (key, chid)
                    )
                config[key] = value
            elif kind == "bool":
                config[key] = bool(value)
            else:
                config[key] = "" if value is None else str(value)
        settings["config"] = config
        self.settings[chid] = settings
        self._save_config()
        return self.getChannelConfig(chid)


_installed = None
_installed_lock = Lock()


def get_installed():
    """
    Return the InstalledChannels singleton, creating it on first use.

    Instantiating it does network I/O (every installed channel's feedlist())
    and imports each installed channel, so it must not happen at module import
    time: that blocked server startup and made this module impossible to
    import in a test or a utility script.

    CherryPy serves each request in its own thread, so creation is guarded.
    Without the lock two requests arriving on a cold server would both build
    the object, duplicating the network work and racing on the settings file.
    """
    global _installed
    if _installed is None:
        with _installed_lock:
            if _installed is None:
                _installed = InstalledChannels()
    return _installed


def list_all():
    channels = get_installed().getAll()
    infolist = []
    for chan in channels:
        infolist.append(info(chan.getId()))
    return infolist


def disable(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    get_installed().disableChannel(chid)
    return list_all()


def enable(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    get_installed().enableChannel(chid)
    return list_all()


def list_enabled():
    enabled = get_installed().getEnabled()
    info = []
    for c in enabled:
        info.append(c.getInfo())
    return info


def info(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    installed = get_installed()
    # A copy. This function adds actions and settings to what it returns, and
    # without a copy those end up written into the channel's own stored info --
    # where list_enabled() also reads from, so one call here gave every other
    # channel listing a Disable button and a settings block it never asked for.
    info = dict(installed.getChannel(chid).getInfo())
    # Where the channel lives in this application. The icon and the title open
    # it -- that is what clicking them does -- so that is what their links have
    # to say, and the server is the one that knows this row is a channel and
    # what it is called. The website is the Source button's job, a button along.
    #
    # Spelled the way channels.html already spells it, so the same destination
    # is not written two different ways in two places.
    info["route"] = "#channel/" + info["id"]
    # A "hint" is what the button says when the pointer rests on it: one word,
    # the other half of what the button does.
    #
    # It used to be a sentence -- "Don't show in Channels" -- on the reasoning
    # that "Disable" alone does not say what is being disabled. It does, here:
    # the row is the channel, the button is on that channel, and the list this
    # manager edits is the one that shows. So the sentence was saying the
    # obvious at length, and the words are now the ones somebody would use.
    #
    # "hint" rather than "title", which reads as the HTML attribute of the same
    # name but resolves inside itemlist's each={ actions } to the row's own
    # title first -- so every Disable button would have said "EZTV".
    channel = installed.getChannel(chid)
    config = installed.getChannelConfig(chid)
    info["configurable"] = channel.configurable
    info["config"] = config
    info["default_feed"] = channel.getDefaultFeedIndex(config)
    if installed.isEnabled(chid):
        info["actions"] = [
            {
                "label": "Disable",
                "type": "disablechannel",
                "hint": "hide",
            }
        ]
    else:
        info["actions"] = [
            {
                "label": "Enable",
                "type": "enablechannel",
                "hint": "show",
            }
        ]
    if channel.configurable:
        # Between Disable and Source: what the channel does, then how it is
        # configured, then where it comes from.
        info["actions"].append(
            {"label": "Settings", "type": "channelsettings", "hint": "configure"}
        )
    # Where the posts come from, beside the button that turns it off rather
    # than in the description, which nothing renders. Added last so it sits
    # after Disable, and only when there is a link to go to.
    if info["link"]:
        # "dest", not "url": inside itemlist's each={ actions } an expression
        # resolves the action's own properties first and the row's after, so a
        # bare "url" finds the row's url for any action that has none of its own
        # -- which is every action except this one. "dest" collides with
        # nothing a row or another action carries.
        info["actions"].append(
            {"label": "Source", "type": "sourcechannel", "dest": info["link"]}
        )
    info["settings"] = installed.getChannelSettings(chid)
    return info


def feedlist(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    channel = get_installed().getChannel(chid)
    # A channel whose list is larger than the one it starts with can build it
    # on demand: the installed-channel list must not wait for the catalogue.
    full = getattr(channel.module, "feedlist_full", None)
    if callable(full):
        return full()
    return channel.getFeeds()


def _reading_a_feed(call, *args):
    """
    Run a call that reads from a channel, turning a busy archive into an error.

    The interface has two states for a feed and they are not the same thing:

        []                              "There are no items for this feed."
        an error                        "The Reddit archive is busy..."

    Only the first of those used to be reachable. A failure to read the archive
    came back as an empty list, so a category that had always had films in it
    looked like it had lost them, and the only evidence was a line in a log
    nobody opens. The distinction is the whole of this function.

    503 rather than 500: the server is fine and the archive is not, and that is
    worth saying in the status as well as in the words.

    Search across every channel is deliberately not routed through here -- it
    catches this itself, so one busy archive skips one channel rather than
    failing the search of forty.
    """
    try:
        return call(*args)
    except ArchiveBusy as exc:
        raise ApiError(str(exc), status=ApiError._SERVICE_UNAVAILABLE) from exc


def _listing(call, *args):
    """
    One channel listing: the rows and what the block list hid from them.

    A channel that knows no block list returns its rows alone; its report is
    None rather than a made-up zero.
    """
    result = _reading_a_feed(call, *args)
    if isinstance(result, tuple):
        return result
    return result, None


def feed(chid=None, idx=None):
    if chid is None or idx is None:
        raise ApiError("Both Channel ID and feed index must be defined")
    installed = get_installed()
    channel = installed.getChannel(chid)
    items, blocked = _listing(
        channel.getFeed, idx, installed.getChannelConfig(chid)
    )
    return {"items": items, "blocked": blocked}


def feed_by_name(chid=None, idx=None, name=None):
    if chid is None or idx is None or name is None:
        raise ApiError("All of Channel ID, feed index and name must be defined")
    installed = get_installed()
    channel = installed.getChannel(chid)
    items, blocked = _listing(
        channel.getFeedByName, idx, name, installed.getChannelConfig(chid)
    )
    return {"items": items, "blocked": blocked}


def search(chid=None, q=None):
    if chid is None or q is None:
        raise ApiError("Both Channel ID and search query must be defined")
    installed = get_installed()
    channel = installed.getChannel(chid)
    items, blocked = _listing(
        channel.search, q, installed.getChannelConfig(chid)
    )
    return {"items": items, "blocked": blocked}


def showmore(chid=None, link=None):
    if chid is None or link is None:
        raise ApiError("Both channel ID and link must be defined")
    installed = get_installed()
    return installed.getChannel(chid).showmore(
        link, installed.getChannelConfig(chid)
    )


def config(chid=None):
    """A channel's settings schema and effective values, for the settings page."""
    if chid is None:
        raise ApiError("Channel ID is missing")
    installed = get_installed()
    channel = installed.getChannel(chid)
    schema = []
    for key, spec in channel.getConfigSchema(expand=True).items():
        row = dict(spec)
        row["key"] = key
        schema.append(row)
    return {
        "id": chid,
        "title": channel.getTitle(),
        "schema": schema,
        "values": installed.getChannelConfig(chid),
    }


def set_config(chid=None, values=None):
    """Save one channel's options, on this installation only."""
    if chid is None:
        raise ApiError("Channel ID is missing")
    if values is None:
        raise ApiError("No settings were given")
    get_installed().setChannelConfig(chid, values)
    return config(chid)


def _search_thread(queue, chid, q):
    results = []
    try:
        results = search(chid, q)["items"]
    except Exception:
        pass
    queue.put((info(chid)["title"], results))


def search_all(q=None):
    if q is None:
        raise ApiError("Search requires query")
    enabled = list_enabled()
    threads = []
    queue = Queue(len(enabled))
    for chan in enabled:
        if chan["search"]:
            th = Thread(target=_search_thread, args=(queue, chan["id"], q))
            th.start()
            threads.append(th)

    results = []
    for thread in threads:
        thread.join()
        r = queue.get()
        if len(r[1]) > 0:
            results.append(r)
    return results

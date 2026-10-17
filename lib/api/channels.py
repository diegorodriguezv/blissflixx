import re
import glob
import os
import shutil
import sys
import traceback
from os import path
from queue import Queue
from threading import Lock, Thread

from ..locations import CHAN_PATH, PLUGIN_PATH, ROOT_PATH
from ..settings import load, save
from .common import ApiError

#: The one channel backend that reads from a third party, imported for the
#: failure it raises when that third party cannot answer. A dependency being
#: unavailable is not a bug in this server and must not be reported as one, so it
#: is translated here into an ApiError that says what happened in words the
#: interface can put on screen.
from ..chanutils.reddit import ArchiveBusy

CHANID_GLOB = "bfch_*"

# Installed channels are imported by bare module name, so their directories
# have to be importable.
sys.path.append(CHAN_PATH)
sys.path.append(PLUGIN_PATH)


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


class Channel:
    def __init__(self, cpath, plugin):
        chid = path.basename(cpath)
        module = __import__(chid, globals(), locals(), [])
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
            # The channel's website. Carried as "link" and not as "url": a media
            # item's "url" is the thing to play, and the interface plays what it
            # finds there. A channel is not playable and putting its website in
            # that key would mean a click on a channel in the manager asking
            # yt-dlp to play bbc.co.uk.
            "link": self._website(desc),
        }
        self.chid = chid
        self.module = module
        self.cpath = cpath

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

    def getPlayItems(self, items):
        items = items.to_dict()
        # fix any missing images
        # and Add channel id to showmore items and actions
        for i in items:
            if "url" in i and i["url"].startswith("showmore://"):
                i["chid"] = self.chid
            if ("img" not in i) or (i["img"] is None):
                i["img"] = self.image
            if "actions" in i:
                for action in i["actions"]:
                    if action["type"] == "showmore":
                        action["chid"] = self.chid

        return items

    def getFeed(self, idx):
        return self.getPlayItems(self.module.feed(idx))

    def getFeedByName(self, idx, name):
        return self.getPlayItems(self.module.feed_by_name(idx, name))

    def search(self, q):
        return self.getPlayItems(self.module.search(q))

    def showmore(self, link):
        return self.getPlayItems(self.module.showmore(link))

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
        cpaths = glob.glob(path.join(CHAN_PATH, CHANID_GLOB))
        for p in cpaths:
            # A directory with no __init__.py is not a channel.
            #
            # It happens after a channel is removed: git deletes the tracked
            # files but leaves the directory when something untracked is still
            # in it, and __pycache__ is exactly that. Python then imports the
            # directory as an implicit namespace package -- an empty module that
            # raises AttributeError on the first attribute read -- so every
            # restart logged a traceback for a channel that had been deliberately
            # deleted. Harmless, because the loader catches it, but it is noise
            # on every boot and it reads as a fault rather than as the absence
            # of something.
            if not path.isfile(path.join(p, "__init__.py")):
                continue
            try:
                channels.append(Channel(p, False))
            except Exception:
                # A broken channel must not take the whole server down, and it
                # need not raise ImportError: a bad selector raises ValueError,
                # a changed API shape raises KeyError, and so on.
                print(f"Failed to load channel {p}")
                traceback.print_exc()
        cpaths = glob.glob(path.join(PLUGIN_PATH, CHANID_GLOB))
        for p in cpaths:
            if not path.isfile(path.join(p, "__init__.py")):
                continue
            try:
                channels.append(Channel(p, True))
            except Exception:
                print(f"Failed to load plugin {p}")
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
    if installed.isEnabled(chid):
        info["actions"] = [{"label": "Disable", "type": "disablechannel"}]
    else:
        info["actions"] = [{"label": "Enable", "type": "enablechannel"}]
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
    return get_installed().getChannel(chid).getFeeds()


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


def feed(chid=None, idx=None):
    if chid is None or idx is None:
        raise ApiError("Both Channel ID and feed index must be defined")
    channel = get_installed().getChannel(chid)
    return _reading_a_feed(channel.getFeed, idx)


def feed_by_name(chid=None, idx=None, name=None):
    if chid is None or idx is None or name is None:
        raise ApiError("All of Channel ID, feed index and name must be defined")
    channel = get_installed().getChannel(chid)
    return _reading_a_feed(channel.getFeedByName, idx, name)


def search(chid=None, q=None):
    if chid is None or q is None:
        raise ApiError("Both Channel ID and search query must be defined")
    channel = get_installed().getChannel(chid)
    return _reading_a_feed(channel.search, q)


def showmore(chid=None, link=None):
    if chid is None or link is None:
        raise ApiError("Both channel ID and link must be defined")
    return get_installed().getChannel(chid).showmore(link)


def _search_thread(queue, chid, q):
    results = []
    try:
        results = search(chid, q)
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

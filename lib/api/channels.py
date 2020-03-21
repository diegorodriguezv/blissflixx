from os import path
from .common import ApiError
from chanutils import get_json
from threading import Thread
from queue import Queue
import glob, locations, settings, os, subprocess, chanutils
import shutil

CHANID_GLOB = "bfch_*"


class Channel:
    def __init__(self, cpath, plugin):
        chid = path.basename(cpath)
        module = __import__(chid, globals(), locals(), [], 0)
        name = module.name()
        subtitle = module.description()
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
            "subtitle": subtitle,
            "feeds": feeds,
        }
        self.chid = chid
        self.module = module
        self.cpath = cpath

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
        backup_folder = path.join(locations.ROOT_PATH, folder)
        for p in plugins:
            plugin = path.join(locations.PLUGIN_PATH, p)
            if path.exists(plugin):
                if not path.exists(backup_folder):
                    os.mkdir(backup_folder)
                shutil.move(plugin, backup_folder)

    def _refresh(self):
        channels = []
        cpaths = glob.glob(path.join(locations.CHAN_PATH, CHANID_GLOB))
        for p in cpaths:
            try:
                channels.append(Channel(p, False))
            except ImportError:
                pass
        cpaths = glob.glob(path.join(locations.PLUGIN_PATH, CHANID_GLOB))
        for p in cpaths:
            try:
                channels.append(Channel(p, True))
            except ImportError:
                pass
        # Ignore channels with no image
        channels = [chan for chan in channels if chan.imageExists()]
        self.channels = sorted(channels, key=lambda chan: chan.getTitle().upper())
        self.settings = settings.load("channels")

    def _set_config(self, chid, key, value):
        settings = self.getChannelSettings(chid)
        settings[key] = value
        self.settings[chid] = settings
        self._save_config()

    def _save_config(self):
        settings.save("channels", self.settings)

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


installed = InstalledChannels()


def list_all():
    channels = installed.getAll()
    infolist = []
    for chan in channels:
        infolist.append(info(chan.getId()))
    return infolist


def disable(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    installed.disableChannel(chid)
    return list_all()


def enable(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    installed.enableChannel(chid)
    return list_all()


def list_enabled():
    enabled = installed.getEnabled()
    info = []
    for c in enabled:
        info.append(c.getInfo())
    return info


def info(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    info = installed.getChannel(chid).getInfo()
    if installed.isEnabled(chid):
        info["actions"] = [{"label": "Disable", "type": "disablechannel"}]
    else:
        info["actions"] = [{"label": "Enable", "type": "enablechannel"}]
    info["settings"] = installed.getChannelSettings(chid)
    return info


def feedlist(chid=None):
    if chid is None:
        raise ApiError("Channel ID is missing")
    return installed.getChannel(chid).getFeeds()


def feed(chid=None, idx=None):
    if chid is None or idx is None:
        raise ApiError("Both Channel ID and feed index must be defined")
    return installed.getChannel(chid).getFeed(idx)


def feed_by_name(chid=None, idx=None, name=None):
    if chid is None or idx is None or name is None:
        raise ApiError("All of Channel ID, feed index and name must be defined")
    return installed.getChannel(chid).getFeedByName(idx, name)


def search(chid=None, q=None):
    if chid is None or q is None:
        raise ApiError("Both Channel ID and search query must be defined")
    return installed.getChannel(chid).search(q)


def showmore(chid=None, link=None):
    if chid is None or link is None:
        raise ApiError("Both channel ID and link must be defined")
    return installed.getChannel(chid).showmore(link)


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

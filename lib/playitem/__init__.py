import re
import urllib.parse

from ..chanutils.chanutils import movie_title_year, series_season_episode
from ..chanutils.torrent import is_main, is_torrent
from ..settings import load

#: A title with the year at the end and nothing after it.
_TRAILING_YEAR = re.compile(r"(.*\S)\s+([12][90]\d\d)$")


class Action:
    # Subclasses implement to_dict(). This base class previously declared a
    # get_dict() method that returned None and was never called anywhere.
    pass


class AddPlaylistAction(Action):
    def __init__(self):
        pass

    def to_dict(self):
        return {"type": "addplaylist", "label": "Add To Playlist"}


class PlayWithSubsAction(Action):
    def __init__(self):
        pass

    def to_dict(self):
        return {"type": "playwithsubs", "label": "Play With Subtitles"}


class RemoveFromPlaylistAction(Action):
    def __init__(self):
        pass

    def to_dict(self):
        return {"type": "delplaylistitem", "label": "Remove From Playlist"}


class EditPlaylistItemAction(Action):
    def __init__(self):
        pass

    def to_dict(self):
        return {"type": "editplaylistitem", "label": "Edit Item"}


class ShowmoreAction(Action):
    def __init__(self, label, link, title):
        self.label = label
        self.link = link
        self.title = title

    def to_dict(self):
        return {
            "type": "showmore",
            "label": self.label,
            "link": self.link,
            "title": self.title,
        }


class MoreEpisodesAction(ShowmoreAction):
    def __init__(self, link, title):
        ShowmoreAction.__init__(self, "More Episodes...", link, title)


class TorrentFilesAction(Action):
    def __init__(self, link, title):
        self.link = link
        self.title = title

    def to_dict(self):
        # "torrfiles" is handled by html/tags/itemlist.html, which routes to
        # html/pages/torrfiles.html and calls the torrent/files API.
        return {
            "type": "torrfiles",
            "label": "View Files...",
            "link": self.link,
            "title": self.title,
        }


def _with_year(name):
    """A name and its year, in the form a search box understands."""
    parsed = movie_title_year(name)
    if "year" in parsed:
        return "%s (%d)" % (parsed["title"], parsed["year"])
    # A series name straight off a release has the year at the end, with no
    # quality tag after it for movie_title_year's pattern to hold on to.
    m = _TRAILING_YEAR.match(parsed["title"].strip())
    if m:
        return "%s (%s)" % (m.group(1), m.group(2))
    return parsed["title"]


def imdb_query(title):
    """
    What to look up on IMDb, from a release name.

    The name on the row is a file name, not a title: quality, source, codec and
    release group are all in it, and searching for it finds nothing.

        Spider-Man.Brand.New.Day.2026.1080p.WEBRip.10Bit.DDP5.1.x265-NeoNoir
        -> Spider-Man Brand New Day (2026)

    An episode is searched by its series -- the episode itself has no page of
    its own on IMDb -- and that series may carry a year, which is worth keeping
    because it tells an original from a remake.
    """
    series = series_season_episode(title)
    if "episode" in series:
        return _with_year(series["series"])
    return _with_year(title)


class ImdbSearchAction(Action):
    """
    A looking glass on a torrent: what this is, on IMDb.

    Icon only, because the row already carries three text buttons and this is
    the one that leaves the page. "dest" is the address the link goes to, which
    is what gives it a real href -- middle-click and the browser's own hover
    line work without going through the click handler -- and the same key the
    channel manager's Source button uses.
    """

    def __init__(self, title):
        self.title = title

    def to_dict(self):
        return {
            "type": "imdbsearch",
            "label": "",
            "hint": "Search IMDb",
            "icon": "fa-search",
            "dest": "https://www.imdb.com/find/?q="
            + urllib.parse.quote(imdb_query(self.title)),
        }


# This class was previously defined twice in this module. Both bodies executed
# on import and the second silently shadowed the first, so the variant below
# was the one actually in use. It reported type "showmore", which routed
# "View Files..." through channels.showmore instead of torrent/files.
# class TorrentFilesAction(Action):
#     def __init__(self, link, title):
#         self.link = link
#         self.title = title
#
#     def to_dict(self):
#         return {
#             "type": "showmore",
#             "label": "View Files...",
#             "link": self.link,
#             "title": self.title,
#         }


class ActionList:
    def __init__(self, action=None):
        self.alist = []
        if action is not None:
            self.alist.append(action)

    def add(self, action):
        self.alist.append(action)

    def empty(self):
        if len(self.alist) == 0:
            return True
        else:
            return False

    def to_dict(self):
        if len(self.alist) == 0:
            return None
        dlist = []
        for a in self.alist:
            dlist.append(a.to_dict())
        return dlist


class PlayItem:
    def __init__(self, title, img, url, subtitle=None, synopsis=None, subs=None):
        self.title = title
        self.img = img
        self.url = url
        self.subtitle = subtitle
        self.synopsis = synopsis
        self.subs = self._set_subs_lang(subs)
        self.actions = ActionList()
        self.add_default_actions()

    def _set_subs_lang(self, subs):
        # A torrent's own subtitle files have no language to look up: the file
        # is the subtitle. Only an online search carries one.
        if subs is not None and "torrent" not in subs:
            sub_settings = load("subtitles")
            if "lang" in sub_settings:
                subs["lang"] = sub_settings["lang"]
            else:
                subs["lang"] = "eng"
        return subs

    def add_default_actions(self):
        if self.subs is not None:
            self.add_action(PlayWithSubsAction())
        self.add_action(AddPlaylistAction())
        if is_main(self.url):
            self.add_action(TorrentFilesAction(self.url, self.title))
        if is_torrent(self.url):
            # Only a torrent, because the name on the row is a release name
            # there and a search can be made of it. A channel item's title is
            # already the thing itself, and the page it came from is what the
            # row links to.
            self.add_action(ImdbSearchAction(self.title))

    def add_action(self, action):
        self.actions.add(action)

    def to_dict(self):
        d = {"title": self.title, "img": self.img, "url": self.url}
        if self.subtitle is not None:
            d["subtitle"] = self.subtitle
        if self.synopsis is not None:
            d["synopsis"] = self.synopsis
        if self.subs is not None:
            d["subs"] = self.subs
        if not self.actions.empty():
            d["actions"] = self.actions.to_dict()
        return d


class LiveStreamPlayItem(PlayItem):
    def __init__(self, title, img, url, subtitle=None):
        PlayItem.__init__(self, title, img, url, subtitle)


class PlaylistItem(PlayItem):
    def __init__(self, item, playlist, itemnum, remote):
        title = item["title"]
        img = item["img"]
        url = item["url"]
        subtitle = None
        if "subtitle" in item:
            subtitle = item["subtitle"]
        synopsis = None
        if "synopsis" in item:
            synopsis = item["synopsis"]
        subs = None
        if "subs" in item:
            subs = item["subs"]
        self.playlist = playlist
        self.itemnum = itemnum
        self.remote = remote
        self.target = None
        if "target" in item:
            self.target = item["target"]
        PlayItem.__init__(self, title, img, url, subtitle, synopsis, subs)

    def add_default_actions(self):
        PlayItem.add_default_actions(self)
        if not self.remote:
            self.add_action(EditPlaylistItemAction())
            self.add_action(RemoveFromPlaylistAction())

    def to_dict(self):
        d = PlayItem.to_dict(self)
        d["playlist"] = self.playlist
        d["itemnum"] = self.itemnum
        if self.target is not None:
            d["target"] = self.target
        return d


class SearchItem(PlayItem):
    def __init__(self, title, img, subtitle=None, synopsis=None):
        url = "search://" + title
        PlayItem.__init__(self, title, img, url, subtitle, synopsis)

    def add_default_actions(self):
        pass


class ShowMoreItem(PlayItem):
    def __init__(self, title, img, url, subtitle=None, synopsis=None):
        url = "showmore://" + url
        PlayItem.__init__(self, title, img, url, subtitle, synopsis)

    def add_default_actions(self):
        pass


class PlayItemList:
    def __init__(self):
        self.itemlist = []

    def add(self, item):
        self.itemlist.append(item)

    def to_dict(self):
        dlist = []
        for item in self.itemlist:
            dlist.append(item.to_dict())
        return dlist

    def to_list(self):
        return self.itemlist

from lib.chanutils import get_json, post_json
from lib.playitem import LiveStreamPlayItem, PlayItemList

_CLIENT_ID = "yicd1x2uyr" + "hazbdc4n7zba4yzrjlx0"
_CLIENT_SECRET = "9dbytqxr6vpza42wexs1zjx" + "2cmjq56"
_STREAM_URL = "https://api.twitch.tv/helix/streams"
_SEARCH_URL = "https://api.twitch.tv/helix/search/channels"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
__access_headers = dict()

#: The feeds are a fixed set of popular games/categories, not a live "top
#: games" call. feedlist() runs while the installed-channel list is built and
#: must not wait on Twitch or depend on a token. The streams inside a feed still
#: come from Twitch.
#:
#: Each feed carries a stable "name" so routes can resolve by name (not just
#: positional index). This keeps clicks opening the intended game even if the
#: list is reordered or expanded later.
_FEEDLIST = [
    {"title": "StarCraft II", "id": "490422", "name": "starcraft-ii"},
    {"title": "Warcraft III", "id": "12924", "name": "warcraft-iii"},
    {"title": "Just Chatting", "id": "509658", "name": "just-chatting"},
    {"title": "League of Legends", "id": "21779", "name": "league-of-legends"},
    {"title": "Fortnite", "id": "33214", "name": "fortnite"},
    {"title": "Grand Theft Auto V", "id": "32982", "name": "grand-theft-auto-v"},
    {"title": "Valorant", "id": "516575", "name": "valorant"},
    {"title": "Counter-Strike", "id": "32399", "name": "counter-strike"},
    {"title": "Dota 2", "id": "29595", "name": "dota-2"},
    {"title": "Minecraft", "id": "27471", "name": "minecraft"},
    {"title": "World of Warcraft", "id": "18122", "name": "world-of-warcraft"},
    {"title": "Overwatch", "id": "515025", "name": "overwatch"},
    {"title": "Apex Legends", "id": "511224", "name": "apex-legends"},
    {"title": "Call of Duty: Warzone", "id": "512710", "name": "call-of-duty-warzone"},
    {"title": "AION 2", "id": "771448419", "name": "aion-2"},
    {"title": "ROB RUN", "id": "1753787976", "name": "rob-run"},
    {"title": "The Legend of Zelda: Ocarina of Time", "id": "2067181260", "name": "zelda-ocarina-of-time"},
    {"title": "Mimic Party", "id": "1544653089", "name": "mimic-party"},
    {"title": "Call of Duty: Modern Warfare 4", "id": "1478650472", "name": "call-of-duty-modern-warfare-4"},
    {"title": "Deadlock", "id": "2132205352", "name": "deadlock"},
    {"title": "IRL", "id": "509672", "name": "irl"},
    {"title": "Slots", "id": "498566", "name": "slots"},
    {"title": "Dead by Daylight", "id": "491487", "name": "dead-by-daylight"},
    {"title": "Tibia", "id": "19619", "name": "tibia"},
    {"title": "Phantom Blade Zero", "id": "306019403", "name": "phantom-blade-zero"},
    {"title": "Always On", "id": "499973", "name": "always-on"},
    {"title": "Old School RuneScape", "id": "459931", "name": "old-school-runescape"},
    {"title": "Can 'Em All", "id": "1383161249", "name": "can-em-all"},
    {"title": "Potato Please", "id": "1514404776", "name": "potato-please"},
    {"title": "ARC Raiders", "id": "464339927", "name": "arc-raiders"},
    {"title": "Enshrouded", "id": "737987407", "name": "enshrouded"},
    {"title": "ASMR", "id": "509659", "name": "asmr"},
    {"title": "Marvel Rivals", "id": "1264310518", "name": "marvel-rivals"},
    {"title": "Hunt: Showdown 1896", "id": "500188", "name": "hunt-showdown-1896"},
    {"title": "Virtual Casino", "id": "29452", "name": "virtual-casino"},
    {"title": "Art", "id": "509660", "name": "art"},
    {"title": "EA Sports FC 27", "id": "123184772", "name": "ea-sports-fc-27"},
    {"title": "Rocket League", "id": "30921", "name": "rocket-league"},
    {"title": "ROBLOX", "id": "23020", "name": "roblox"},
    {"title": "nanos world", "id": "1763263", "name": "nanos-world"},
    {"title": "RV There Yet?", "id": "1517377137", "name": "rv-there-yet"},
    {"title": "Genshin Impact", "id": "513181", "name": "genshin-impact"},
    {"title": "Retro", "id": "27284", "name": "retro"},
    {"title": "Finals Week", "id": "1032872922", "name": "finals-week"},
    {"title": "PUBG: BATTLEGROUNDS", "id": "493057", "name": "pubg-battlegrounds"},
    {"title": "Rainbow Six Siege", "id": "460630", "name": "rainbow-six-siege"},
    {"title": "VRChat", "id": "499003", "name": "vrchat"},
    {"title": "QSMP", "id": "1422023940", "name": "qsmp"},
    {"title": "Tom Clancy's The Division 2", "id": "504463", "name": "the-division-2"},
    {"title": "Probably Stolen", "id": "366412048", "name": "probably-stolen"},
    {"title": "Music", "id": "26936", "name": "music"},
    {"title": "DJs", "id": "1669431183", "name": "djs"},
    {"title": "Grand Theft Auto IV", "id": "18707", "name": "grand-theft-auto-iv"},
    {"title": "NBA 2K27", "id": "602040818", "name": "nba-2k27"},
    {"title": "World of Tanks", "id": "27546", "name": "world-of-tanks"},
    {"title": "Geometry Dash", "id": "460951", "name": "geometry-dash"},
    {"title": "Animals, Aquariums, and Zoos", "id": "272263131", "name": "animals-aquariums-zoos"},
    {"title": "Escape from Tarkov", "id": "491931", "name": "escape-from-tarkov"},
    {"title": "Queen's Domain", "id": "1934479857", "name": "queens-domain"},
    {"title": "Gray Zone Warfare", "id": "2106425216", "name": "gray-zone-warfare"},
    {"title": "Magic: The Gathering", "id": "2748", "name": "magic-the-gathering"},
    {"title": "WARDOGS", "id": "743893402", "name": "wardogs"},
]


def get_acccess_token():
    global __access_headers
    response = post_json(
        _TOKEN_URL,
        {
            "client_id": _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    access_token = response["access_token"]
    __access_headers = {
        "Authorization": "Bearer " + access_token,
        "Client-Id": _CLIENT_ID,
    }


def retry_if_token_expired(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except:
            get_acccess_token()
            kwargs.update({"headers": __access_headers})
        return func(*args, **kwargs)

    return wrapper


# apply decorator manually
get_json = retry_if_token_expired(get_json)


def name():
    return "Twitch"


def image():
    return "icon.png"


def description():
    return "Twitch Channel (<a target='_blank' href='http://www.twitch.tv/'>http://www.twitch.tv/</a>)."


def feedlist():
    return _FEEDLIST


def _feed_index_by_name(name):
    if not name:
        return None
    name = str(name)
    for i, feed in enumerate(_FEEDLIST):
        if feed.get("name") == name or feed.get("id") == name:
            return i
    name_l = name.lower()
    for i, feed in enumerate(_FEEDLIST):
        if feed.get("title", "").lower() == name_l:
            return i
    return None


def _streams_for_game(game_id):
    streams = get_json(
        _STREAM_URL, {"first": "50", "game_id": game_id}, headers=__access_headers
    )["data"]
    return _extract(streams)


def feed(idx):
    idx = int(idx)
    if idx < 0 or idx >= len(_FEEDLIST):
        idx = 0
    game_id = _FEEDLIST[idx]["id"]
    return _streams_for_game(game_id)


def feed_by_name(idx, name):
    """Resolve by stable feed name/id when possible, so clicks stay correct even
    if feed order changes."""
    i = _feed_index_by_name(name)
    if i is None:
        try:
            i = int(idx)
        except Exception:
            i = 0
    if i < 0 or i >= len(_FEEDLIST):
        i = 0
    return _streams_for_game(_FEEDLIST[i]["id"])


def search(q):
    streams = get_json(
        _SEARCH_URL,
        {"first": 50, "query": q, "live_only": True},
        headers=__access_headers,
    )["data"]
    return _extract_channels(streams)


def _extract_channels(stream_json):
    results = PlayItemList()
    for stream in stream_json:
        title = stream["display_name"] + " - " + stream["title"]
        game = stream["game_name"]
        tags = stream["tags"]
        subtitle = "tags: " + ", ".join(tags) + "<br>" + game
        img = (
            stream["thumbnail_url"].replace("{width}", "300").replace("{height}", "200")
        )
        url = "https://www.twitch.tv/" + stream["broadcaster_login"]
        results.add(LiveStreamPlayItem(title, img, url, subtitle))
    return results


def _extract(stream_json):
    results = PlayItemList()
    for stream in stream_json:
        title = stream["user_name"] + " - " + stream["title"]
        status = stream["type"]
        viewers = stream["viewer_count"]
        subtitle = "viewers: " + str(viewers) + "<br>" + status
        img = (
            stream["thumbnail_url"].replace("{width}", "300").replace("{height}", "200")
        )
        url = "https://www.twitch.tv/" + stream["user_name"]
        results.add(LiveStreamPlayItem(title, img, url, subtitle))
    return results

from lib.chanutils import get_json, post_json
from lib.playitem import LiveStreamPlayItem, PlayItemList

_CLIENT_ID = "yicd1x2uyr" + "hazbdc4n7zba4yzrjlx0"
_CLIENT_SECRET = "9dbytqxr6vpza42wexs1zjx" + "2cmjq56"
_STREAM_URL = "https://api.twitch.tv/helix/streams"
_SEARCH_URL = "https://api.twitch.tv/helix/search/channels"
_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
__access_headers = dict()

#: The feeds are a fixed set of the games and categories people watch most --
#: the top sixty at the time this was written -- not a live "top games" call.
#: feedlist() is called while the installed-channel list is being built, and
#: that must not wait on Twitch: a slow or rejected token there held every
#: channel's page at "loading" and, on a rejection, dropped the channel from
#: the list entirely. The live part of this channel is the streams inside a
#: feed; those still come from Twitch. The first twelve keep their positions so
#: a route saved before the list grew still opens the same feed.
_FEEDLIST = [
    {"title": "Just Chatting", "id": "509658"},
    {"title": "League of Legends", "id": "21779"},
    {"title": "Fortnite", "id": "33214"},
    {"title": "Grand Theft Auto V", "id": "32982"},
    {"title": "Valorant", "id": "516575"},
    {"title": "Counter-Strike", "id": "32399"},
    {"title": "Dota 2", "id": "29595"},
    {"title": "Minecraft", "id": "27471"},
    {"title": "World of Warcraft", "id": "18122"},
    {"title": "Overwatch", "id": "515025"},
    {"title": "Apex Legends", "id": "511224"},
    {"title": "Call of Duty: Warzone", "id": "512710"},
    {"title": "AION 2", "id": "771448419"},
    {"title": "ROB RUN", "id": "1753787976"},
    {"title": "The Legend of Zelda: Ocarina of Time", "id": "2067181260"},
    {"title": "Mimic Party", "id": "1544653089"},
    {"title": "Call of Duty: Modern Warfare 4", "id": "1478650472"},
    {"title": "Deadlock", "id": "2132205352"},
    {"title": "IRL", "id": "509672"},
    {"title": "Slots", "id": "498566"},
    {"title": "Dead by Daylight", "id": "491487"},
    {"title": "Tibia", "id": "19619"},
    {"title": "Phantom Blade Zero", "id": "306019403"},
    {"title": "Always On", "id": "499973"},
    {"title": "Old School RuneScape", "id": "459931"},
    {"title": "Can 'Em All", "id": "1383161249"},
    {"title": "Potato Please", "id": "1514404776"},
    {"title": "ARC Raiders", "id": "464339927"},
    {"title": "Enshrouded", "id": "737987407"},
    {"title": "ASMR", "id": "509659"},
    {"title": "Marvel Rivals", "id": "1264310518"},
    {"title": "Hunt: Showdown 1896", "id": "500188"},
    {"title": "Virtual Casino", "id": "29452"},
    {"title": "Art", "id": "509660"},
    {"title": "EA Sports FC 27", "id": "123184772"},
    {"title": "Rocket League", "id": "30921"},
    {"title": "ROBLOX", "id": "23020"},
    {"title": "nanos world", "id": "1763263"},
    {"title": "RV There Yet?", "id": "1517377137"},
    {"title": "Genshin Impact", "id": "513181"},
    {"title": "Retro", "id": "27284"},
    {"title": "Finals Week", "id": "1032872922"},
    {"title": "PUBG: BATTLEGROUNDS", "id": "493057"},
    {"title": "Rainbow Six Siege", "id": "460630"},
    {"title": "VRChat", "id": "499003"},
    {"title": "QSMP", "id": "1422023940"},
    {"title": "Tom Clancy's The Division 2", "id": "504463"},
    {"title": "Probably Stolen", "id": "366412048"},
    {"title": "Music", "id": "26936"},
    {"title": "DJs", "id": "1669431183"},
    {"title": "Grand Theft Auto IV", "id": "18707"},
    {"title": "NBA 2K27", "id": "602040818"},
    {"title": "World of Tanks", "id": "27546"},
    {"title": "Geometry Dash", "id": "460951"},
    {"title": "Animals, Aquariums, and Zoos", "id": "272263131"},
    {"title": "Escape from Tarkov", "id": "491931"},
    {"title": "Queen's Domain", "id": "1934479857"},
    {"title": "Gray Zone Warfare", "id": "2106425216"},
    {"title": "Magic: The Gathering", "id": "2748"},
    {"title": "WARDOGS", "id": "743893402"},
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


def feed(idx):
    game_id = _FEEDLIST[idx]["id"]
    streams = get_json(
        _STREAM_URL, {"first": "50", "game_id": game_id}, headers=__access_headers
    )["data"]
    return _extract(streams)


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

import base64
import re
import urllib.parse

from ..chanutils import get

hash_re = re.compile("xt=urn:btih:([A-Za-z0-9]+)")
base32_re = re.compile("[A-Z2-7]{32}")
valid_re = re.compile("[A-F0-9]{40}")

torr_sites = ("torcache.net", "zoink.it")


def torrent_from_hash(hashid):
    path = "/torrent/" + hashid + ".torrent"
    for site in torr_sites:
        try:
            r = get("http://" + site + path)
            return r.content
        except Exception:
            pass
    return None


def magnet2torrent(link):
    matches = hash_re.search(link)
    if not matches or len(matches.groups()) != 1:
        raise Exception("Unable to find magnet hash")
    hashid = matches.group(1).upper()

    # If hash is base32, convert it to base16
    if len(hashid) == 32 and base32_re.search(hashid):
        s = base64.b32decode(hashid)
        hashid = base64.b16encode(s)
    elif not (len(hashid) == 40 and valid_re.search(hashid)):
        raise Exception("Invalid magnet hash")

    return torrent_from_hash(hashid)


def subtitle(size, seeds, peers):
    subtitle = "Size: " + str(size)
    subtitle = subtitle + ", Seeds: " + str(seeds)
    subtitle = subtitle + ", Peers: " + str(peers)
    return subtitle


def is_torrent(url):
    obj = urllib.parse.urlparse(url)
    if obj.path.endswith(".torrent") or url.startswith("magnet:"):
        return True
    else:
        return False


def torrent_idx(url):
    obj = urllib.parse.urlparse(url)
    idx = None
    if obj.query:
        params = urllib.parse.parse_qs(obj.query)
        if "bf_torr_idx" in params:
            idx = params["bf_torr_idx"][0]
    if idx is not None:
        idx = int(idx)
    return idx


def is_main(url):
    return torrent_idx(url) == -1

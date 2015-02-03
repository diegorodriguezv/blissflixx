from os import path

LIB_PATH = path.split(path.abspath(path.dirname(__file__)))[0]
ROOT_PATH = path.split(LIB_PATH)[0]
HTML_PATH = path.join(ROOT_PATH, "html")
EXTN_PATH = path.join(ROOT_PATH, "extn")
YTUBE_PATH = path.join(EXTN_PATH, "youtube-dl")
DATA_PATH = path.join(ROOT_PATH, "data")
PLIST_PATH = path.join(DATA_PATH, "playlists")
CHAN_PATH = EXTN_PATH

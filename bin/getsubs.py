#!/usr/bin/env python3

import io
import json
import socket
import sys
import zipfile
import zlib
from argparse import ArgumentParser
from base64 import b64decode
from http.client import HTTPConnection
from os import path
from xmlrpc.client import ServerProxy, Transport

import requests

OUT_DIR = "/tmp/blissflixx"

langMap = {
    "bul": "bulgarian",
    "chi": "chinese",
    "hrv": "croatian",
    "dan": "danish",
    "dut": "dutch",
    "eng": "english",
    "fin": "finnish",
    "fre": "french",
    "ger": "german",
    "ell": "greek",
    "hun": "hungarian",
    "ita": "italian",
    "mac": "macedonian",
    "pol": "polish",
    "por": "portuguese",
    "srp": "serbian",
    "slv": "slovenian",
    "spa": "spanish",
}


def error(msg):
    print(('{"error":"' + msg + '"}'))
    sys.exit(1)


class TimeoutTransport(Transport, object):
    def __init__(self, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, *args, **kwargs):
        super(TimeoutTransport, self).__init__(*args, **kwargs)
        self.timeout = timeout

    def make_connection(self, host):
        h = HTTPConnection(host, timeout=self.timeout)
        return h


class OpensubSession(object):
    def __init__(self):
        self.con = ServerProxy(
            "http://api.opensubtitles.org/xml-rpc",
            transport=TimeoutTransport(10),
            headers=(),
        )
        # Login and use subliminal user-agent
        r = self.con.LogIn("blissflixx", "PREV-BLUE-SCOPE-FEAR", "eng", "VLSub 0.11.1")
        self.ok(r)
        self.token = r["token"]

    def search(self, params):
        r = self.con.SearchSubtitles(self.token, params)
        self.ok(r)
        return r["data"]

    def decode(self, content, lang):
        encodings = ["utf-8"]

        # add language-specific encodings
        if lang == "zho":
            encodings.extend(["gb18030", "big5"])
        elif lang == "jpn":
            encodings.append("shift-jis")
        elif lang == "ara":
            encodings.append("windows-1256")
        elif lang == "heb":
            encodings.append("windows-1255")
        elif lang == "tur":
            encodings.extend(["iso-8859-9", "windows-1254"])
        elif lang == "pol" or lang == "hrv":
            # Eastern European Group 1
            encodings.extend(["windows-1250"])
        elif lang == "bul":
            # Eastern European Group 2
            encodings.extend(["windows-1251"])
        else:
            # Western European (windows-1252)
            encodings.append("latin-1")

        for encoding in encodings:
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                pass

        error("Could not decode subtitles")

    def download(self, lang, subid, subname):
        r = self.con.DownloadSubtitles(self.token, [subid])
        self.ok(r)
        if not r["data"]:
            return error("Unable to download subs")
        content = zlib.decompress(b64decode(r["data"][0]["data"]), 47)
        # Fix line endings
        content = content.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        text = self.decode(content, lang)
        subs_path = path.join(OUT_DIR, subname)
        with io.open(subs_path, "w", encoding="utf-8") as f:
            f.write(text)
        return subs_path

    def ok(self, r):
        status_code = int(r["status"][:3])
        if status_code > 301:
            error("Opensubtitles returned: " + str(status_code))


def candidates(args):
    """
    What OpenSubtitles has for this film, as a list to choose from.

    The single-subtitle path below picks for the person -- YIFY first, then the
    best rating -- which is right when nobody is looking and wrong when they
    are: a release can have a normal subtitle, a hearing-impaired one and a
    third someone prefers, and all three are one press apart.
    """
    sess = OpensubSession()
    if args.imdb:
        imdb = args.imdb[2:] if args.imdb[0:2] == "tt" else args.imdb
        params = [{"imdbid": imdb, "sublanguageid": args.lang}]
    elif args.season and args.episode:
        params = [
            {
                "query": args.title,
                "season": args.season,
                "episode": args.episode,
                "sublanguageid": args.lang,
            }
        ]
    else:
        query = args.title
        if args.year:
            query = query + " (" + str(args.year) + ")"
        params = [{"query": query, "sublanguageid": args.lang}]
    results = sess.search(params) or []
    found = []
    for r in results:
        if r.get("SubFormat") != "srt":
            continue
        if (
            args.year
            and _searched_as_movie(args)
            and int(r.get("MovieYear") or 0) != args.year
        ):
            continue
        found.append(
            {
                "id": r["IDSubtitleFile"],
                "name": r["SubFileName"],
                "lang": r.get("SubLanguageID", args.lang),
                "rating": r.get("SubRating"),
                "downloads": r.get("SubDownloadsCnt"),
            }
        )
    found.sort(key=lambda c: float(c["rating"] or 0), reverse=True)
    return found[:25]


def _searched_as_movie(args):
    """Whether this search was for a film. A series search has season and episode."""
    return not (args.season and args.episode)


def chosen_subs(args):
    """
    The subtitles picked from a list, saved under names the caller can find.

    More than one is normal: a release often has a plain subtitle and a
    hearing-impaired one, and both being on the film is the point of being
    allowed to pick two.
    """
    wanted = json.loads(args.choosens)
    lang = wanted.get("lang") or args.lang
    sess = OpensubSession()
    paths = []
    for f in wanted.get("files") or []:
        name = path.basename(f.get("name") or ("subtitle-" + str(f["id"]) + ".srt"))
        try:
            paths.append(sess.download(lang, f["id"], name))
        except SystemExit:
            # One missing subtitle must not lose the others.
            continue
    print(json.dumps({"filenames": paths}))


def opensub_movie_subs(lang, title, year, imdb):
    sess = OpensubSession()
    results = None
    if imdb:
        if imdb[0:2] == "tt":
            imdb = imdb[2:]
        results = sess.search([{"imdbid": imdb, "sublanguageid": lang}])
    else:
        query = title
        if year:
            query = query + " (" + str(year) + ")"
        results = sess.search([{"query": query, "sublanguageid": lang}])
    if not results:
        return None
    maxcnt = -1
    chosen = None
    for r in results:
        if r["SubFormat"] != "srt":
            continue
        if r["MovieKind"] != "movie":
            continue
        if year and int(r["MovieYear"]) != year:
            continue

        # Use YIFY above all others
        if "YIFY" in r["SubFileName"]:
            chosen = r
            break

        # cnt = int(r['SubDownloadsCnt'])
        cnt = float(r["SubRating"])
        if cnt > maxcnt:
            chosen = r
            maxcnt = cnt

    if chosen:
        return sess.download(lang, chosen["IDSubtitleFile"], chosen["SubFileName"])
    else:
        return None


def movie_subs(args):
    return opensub_movie_subs(args.lang, args.title, args.year, args.imdb)


def series_subs(args):
    params = [
        {
            "query": args.title,
            "season": args.season,
            "episode": args.episode,
            "sublanguageid": args.lang,
        }
    ]
    sess = OpensubSession()
    results = sess.search(params)
    if not results:
        return None
    chosen = results[0]
    return sess.download(args.lang, chosen["IDSubtitleFile"], chosen["SubFileName"])


parser = ArgumentParser(description="Get subtitles for movies and TV series")
parser.add_argument("lang", help="Subtitle language")
parser.add_argument(
    "--list",
    action="store_true",
    help="List the candidates instead of choosing one",
)
parser.add_argument(
    "--choosens",
    help='The subtitles picked from a --list, as JSON: {"lang": .., "files": [..]}',
)
parser.add_argument("-t", "--title", help="Movie title")
parser.add_argument("-y", "--year", type=int, help="Movie year")
parser.add_argument("-i", "--imdb", help="IMDB code")
parser.add_argument("-s", "--season", type=int, help="Series season")
parser.add_argument("-e", "--episode", type=int, help="Series episode")
args = parser.parse_args()

is_movie = True
if args.season or args.episode:
    if not (args.title and args.season and args.episode):
        error("Must specify title, season and episode for series")
    is_movie = False
elif not (args.title or args.imdb) and not args.choosens:
    error("Must specify title or imdb code for movie")

if args.list:
    print(json.dumps(candidates(args)))
    sys.exit(0)

if args.choosens:
    chosen_subs(args)
    sys.exit(0)

filename = None
for i in range(3):
    try:
        if is_movie:
            filename = movie_subs(args)
        else:
            filename = series_subs(args)
        break
    except Exception as e:
        filename = "ERROR: " + str(e)

if filename is None:
    print("{}")
elif filename.startswith("ERROR:"):
    error(filename[7:])
elif filename:
    print(('{"filename":"' + filename + '"}'))
else:
    print("{}")

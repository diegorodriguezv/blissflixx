#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import traceback

import cherrypy
from cherrypy._cplogging import LogManager
from cherrypy.lib import static
from cherrypy.process.plugins import Daemonizer

import lib.gitutils as gitutils
import lib.locations as locations

# lib.api is a plain namespace, so its submodules are imported explicitly here
# rather than eagerly from lib/api/__init__.py. Importing them also attaches
# them as attributes of the package, so the getattr dispatch below works.
from lib.api import channels, playlink, playr, playlists, torrent

api_modules = {
    "channels": channels,
    "playlink": playlink,
    "playr": playr,
    "playlists": playlists,
    "torrent": torrent,
}

RESTARTING = False


def check_not_root():
    # Do not allow running as root
    if os.geteuid() == 0:
        print("BlissFlixx should not be run as superuser.")
        print("Please run again but without using sudo.")
        sys.exit(1)


def first_time_install():
    # Check if first time run and need to finish install
    if os.path.exists(locations.YTUBE_PATH):
        return
    cherrypy.log("Finishing Installation. Please wait...")
    gitutils.clone(locations.LIB_PATH, "https://github.com/yt-dlp/yt-dlp.git")

    datapath = locations.DATA_PATH
    playlist_path = os.path.join(datapath, "playlists")
    settings_path = os.path.join(datapath, "settings")
    if not os.path.exists(locations.PLUGIN_PATH):
        os.makedirs(locations.PLUGIN_PATH)
    if not os.path.exists(datapath):
        os.makedirs(datapath)
    if not os.path.exists(playlist_path):
        os.makedirs(playlist_path)
    if not os.path.exists(settings_path):
        os.makedirs(settings_path)


class Api(object):
    def _error(self, status, msg):
        cherrypy.response.status = status
        return {"error": msg}

    def _server(self, fn=None, data=None):
        if fn == "restart":
            global RESTARTING
            RESTARTING = True
            gitutils.pull(locations.YTUBE_PATH)
            gitutils.pull(locations.ROOT_PATH)
            gitutils.pull_subdirs(locations.PLUGIN_PATH)
            os.kill(os.getpid(), signal.SIGUSR2)
        elif fn == "shutdown":
            os.system("sudo shutdown -h 0")
        elif fn == "reboot":
            os.system("sudo shutdown -r 0")
        else:
            return self._error(404, "API Function '" + str(fn) + "' is not defined")

    @cherrypy.expose
    def chanimage(self, chid, img):
        path = os.path.join(locations.CHAN_PATH, chid, img)
        return static.serve_file(path)

    @cherrypy.expose
    def pluginimage(self, chid, img):
        path = os.path.join(locations.PLUGIN_PATH, chid, img)
        return static.serve_file(path)

    @cherrypy.expose
    @cherrypy.tools.json_out()  # type: ignore
    def default(self, modname, fn=None, data=None):
        if modname == "server":
            return self._server(fn, data)
        module = api_modules.get(modname)
        if module is None:
            return self._error(404, "API Module '" + str(modname) + "' is not defined")
        try:
            call = getattr(module, fn)
        except AttributeError:
            return self._error(404, "API Function '" + str(fn) + "' is not defined")
        if not callable(call):
            return self._error(404, "API Function '" + str(fn) + "' is not defined")
        if data is not None:
            datadict = json.loads(data)
        else:
            datadict = {}
        try:
            ret = call(**datadict)
            if ret is not None:
                if RESTARTING and modname == "playr" and fn == "status":
                    ret["Error"] = True
                    ret["Msg"] = "Server Restarting & Updating..."
                    ret["Restart"] = True
                return ret
        except Exception:
            return self._error(500, traceback.format_exc())


def cleanup():
    # Cleanup if previously crashed or was killed
    try:
        shutil.rmtree("/tmp/torrent-stream")
    except Exception:
        pass
    try:
        shutil.rmtree("/tmp/blissflixx")
    except Exception:
        pass
    try:
        home = os.path.expanduser("~")
        os.remove(home + "/.swfinfo")
    except Exception:
        pass
    kill_process("omxplayer")
    kill_process("peerflix")
    kill_process("livestreamer")
    kill_process("dlsrv")


def kill_process(name):
    s = subprocess.check_output("ps -ef | grep " + name, shell=True)
    lines = s.split(b"\n")
    for l in lines:
        items = l.split()
        # Don't kill our own command
        if len(items) > 2 and l.find(b"grep " + bytes(name, "utf-8")) == -1:
            try:
                os.kill(int(items[1]), signal.SIGTERM)
            except Exception:
                pass


class IgnoreStatusLogger(LogManager):
    def __init__(self, *args, **kwargs):
        LogManager.__init__(self, *args, **kwargs)

    def access(self):
        request = cherrypy.serving.request
        # Ignore all status requests as they do nothing but fill up the log
        if request.request_line != "GET /api/playr?fn=status HTTP/1.1":
            return LogManager.access(self)


class Html(object):
    pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", help="Run as daemon process", action="store_true")
    parser.add_argument("--port", type=int, help="Listen port (default 6969)")
    return parser.parse_args(argv)


def mount_trees():
    cherrypy.tree.mount(Api(), "/api")
    cherrypy.tree.mount(
        Html(),
        "/",
        config={
            "/": {
                "tools.staticdir.on": True,
                "tools.staticdir.dir": locations.HTML_PATH,
                "tools.staticdir.index": "index.html",
            },
        },
    )


def main(argv=None):
    check_not_root()
    args = parse_args(argv)
    first_time_install()

    cherrypy.log = IgnoreStatusLogger()
    cherrypy.log("BLISSFLIXX Starting...")

    engine = cherrypy.engine
    if args.daemon:
        Daemonizer(engine).subscribe()

    cleanup()
    mount_trees()

    def exit():
        os.system("stty sane")
        engine.signal_handler.bus.exit()  # type: ignore

    engine.signal_handler.handlers["SIGINT"] = exit  # type: ignore
    engine.signal_handler.handlers["SIGUSR2"] = engine.signal_handler.bus.restart  # type: ignore

    cherrypy.config.update({"server.socket_host": "0.0.0.0"})
    cherrypy.config.update({"server.socket_port": args.port or 6969})
    cherrypy.config.update({"engine.autoreload.on": False})
    cherrypy.config.update({"checker.check_skipped_app_config": False})
    engine.signals.subscribe()  # type: ignore
    engine.start()
    engine.block()


if __name__ == "__main__":
    main()

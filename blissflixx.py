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
from lib.api import channels, playlink, playlists, playr, torrent
from lib.api.common import ApiError
from lib.player import processpipe
from lib.player.pflixproc import BUFFER_DIR

api_modules = {
    "channels": channels,
    "playlink": playlink,
    "playr": playr,
    "playlists": playlists,
    "torrent": torrent,
}

RESTARTING = False

#: Reboot and halt go to systemd directly, and polkit decides whether this
#: process may ask.
#:
#: Not through sudo. sudo cannot work here at all, and the failure looks like
#: nothing to do with permissions:
#:
#:     sudo: error initializing audit plugin sudoers_audit
#:
#: which is sudo failing to initialise at all -- `sudo -l`, which only reads the
#: policy, fails the same way. The cause is that a service has no login session,
#: so its loginuid is unset (4294967295, which is -1):
#:
#:     $ cat /proc/self/loginuid
#:     4294967295
#:
#: libaudit needs a loginuid to record which user invoked a command, finds
#: none, and cannot open the audit log. The same call over ssh, where loginuid is
#: 1000, works every time. So a passwordless sudo grant does not fix it,
#: because sudo never gets as far as reading the grant.
#:
#: polkit is the mechanism for this: it authorises a named action for a subject,
#: and "inactive" is its word for a process with no session -- which is what a
#: service is. configure.sh installs the rule.
#:
#: These are systemctl's verbs. They are not the polkit action ids, which is a
#: separate naming scheme that happens to agree in exactly one of the two cases:
#:
#:     systemctl poweroff                    the verb
#:     org.freedesktop.login1.power-off      the polkit action
#:
#: Both were written as one string and "power-off" was used for both, so the
#: shutdown button ran a verb that does not exist:
#:
#:     Unknown command verb 'power-off', did you mean 'poweroff'?
#:
#: Reboot was never affected, because "reboot" is both. Only the button nobody
#: had tested end to end was broken, which is the worst way for this to happen
#: and the reason the verbs are now checked against systemctl's own list rather
#: than written down and believed.
POWER_ACTIONS = {"reboot": "reboot", "shutdown": "poweroff"}

#: The command that installs them, quoted in full wherever the button is
#: reported as not working.
#:
#: The whole command, and not just the script's name, because the person
#: reading it has a broken button and will not go looking: it has to be
#: something they can copy, paste and have work. So it carries the directory
#: this checkout is in -- the script writes the unit with WorkingDirectory set
#: to wherever configure.sh was run, so "from the checkout" is not a detail that
#: can be left out -- and the -only flag, without which it reinstalls every
#: system dependency first and takes most of an hour on a Pi 3.
POWER_SETUP_COMMAND = "cd %s && sudo ./configure.sh -boot -only" % locations.ROOT_PATH


def check_not_root():
    # Do not allow running as root
    if os.geteuid() == 0:
        print("BlissFlixx should not be run as superuser.")
        print("Please run again but without using sudo.")
        sys.exit(1)


def ensure_data_dirs():
    """
    Create the directories the server writes to.

    Done on every start rather than only on a first run. lib.settings.save()
    writes straight into data/settings with no existence check, so a checkout
    that has yt-dlp but no data directory fails on the first settings write
    rather than at startup. That is not hypothetical: installing by copying the
    tree, or restoring a backup that included lib/yt-dlp, both produce exactly
    that state.
    """
    for path in (
        locations.PLUGIN_PATH,
        locations.DATA_PATH,
        os.path.join(locations.DATA_PATH, "playlists"),
        locations.SETTINGS_PATH,
        os.path.join(locations.DATA_PATH, "downloads"),
    ):
        os.makedirs(path, exist_ok=True)

    # peerflix buffers into data/downloads/torrent-stream on the card, and does
    # not create it. Without this a torrent fails on the first read with an
    # unhelpful error rather than at startup.
    os.makedirs(BUFFER_DIR, exist_ok=True)

    # The scratch directory as well, and for the same reason. Every temporary
    # file BlissFlixx or its dependencies produce lives here now -- downloaded
    # subtitles, the player command fifo, the sockets -- so that there is one
    # directory to look at, and one to clear deliberately rather than as a side
    # effect of something stopping.
    #
    # Deliberately small on purpose. /tmp is a tmpfs sized at half of RAM on any
    # current Pi, so it holds sockets, fifos and ~21KB of peerflix metadata and
    # nothing else. Downloads live on the card.
    os.makedirs(processpipe.TMP_DIR, exist_ok=True)


def first_time_install():
    # The data directories are needed regardless of whether yt-dlp is present.
    ensure_data_dirs()

    if os.path.exists(locations.YTUBE_PATH):
        return
    cherrypy.log("Downloading yt-dlp. Please wait...")
    gitutils.clone(locations.LIB_PATH, "https://github.com/yt-dlp/yt-dlp.git")


class Api:
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
            return self._power("shut down", POWER_ACTIONS["shutdown"])
        elif fn == "reboot":
            return self._power("reboot", POWER_ACTIONS["reboot"])
        else:
            return self._error(404, "API Function '" + str(fn) + "' is not defined")

    def _power(self, verb, action):
        """
        Reboot or halt, by asking systemd and letting polkit decide.

        This used to be os.system("sudo shutdown -r 0"). The server runs from a
        service, so it has no terminal, and sudo cannot ask for a password it
        will never be given. The command failed, os.system said so in a return
        value nobody read, and the interface reported success for a button that
        did nothing. Rebooting had never worked from a service.

        A passwordless sudo grant was tried next, on the reasoning that it is how
        the restart helper already works. The grant installed, parsed, matched,
        and made no difference at all -- because sudo never gets far enough to
        read a grant when it cannot initialise. See POWER_ACTIONS for why it
        cannot initialise here, and why `sudo -l` fails identically.

        The failure is still reported. os.system's status being discarded is what
        made the original invisible: a machine where this is not permitted looks
        exactly like a button nobody pressed.
        """
        try:
            result = subprocess.run(
                ["systemctl", action], capture_output=True, text=True, timeout=30
            )
        except Exception as exc:
            return self._error(500, "Could not %s: %s" % (verb, exc))
        if result.returncode == 0:
            return None
        said = (result.stderr or result.stdout or "").strip().splitlines()
        return self._error(
            500,
            "Could not %s: nothing was done and the machine is as it was.\n\n"
            "This account is not allowed to %s the machine. To allow it, run "
            "this on the Pi:\n\n    %s\n\nsystemctl said: %s"
            % (
                verb,
                action,
                POWER_SETUP_COMMAND,
                " ".join(said[-3:]) if said else "nothing",
            ),
        )

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
        # getattr rejects a non-string name with TypeError rather than
        # AttributeError, so a request with no fn at all would otherwise escape
        # as a 500 traceback.
        if not isinstance(fn, str):
            return self._error(404, "API Function '" + str(fn) + "' is not defined")
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
        except ApiError as exc:
            # A message written for the person using the interface, and the
            # status it belongs to. Returned as-is rather than as a 500 with a
            # traceback, which is right for a bug and wrong for "the Reddit
            # archive is busy" -- the interface puts this text on screen, and it
            # used to put a stack trace there instead, for every ApiError the
            # server raises.
            return self._error(exc.status, str(exc))
        except Exception:
            return self._error(500, traceback.format_exc())


def cleanup():
    """
    Clear out stale processes from a previous run.

    Downloads used to be deleted here too -- /tmp/torrent-stream and all of
    /tmp/blissflixx, on every single start. So nothing survived a restart, let
    alone a stop: a torrent someone had been waiting ten minutes for would be
    gone the next time the server came up. Nothing on disk is removed now.
    Downloads are only ever cleared by the user asking for it.
    """
    try:
        home = os.path.expanduser("~")
        os.remove(home + "/.swfinfo")
    except Exception:
        pass
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


class Html:
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
                # The interface is served with no Cache-Control at all, which
                # leaves the browser to guess: with a Last-Modified and nothing
                # else, a cached copy is reused on a freshness lifetime derived
                # from how old the file is. So after an update the page can keep
                # running the old code, with the server serving the new code to
                # anyone who asks.
                #
                # It is invisible when it happens. The reboot button kept
                # calling Utils.rpc directly off a cached settings.html, so it
                # rebooted the machine and said nothing -- which is exactly
                # what the new bottom bar exists to prevent, defeated by the
                # browser rather than by the code.
                #
                # "no-cache" rather than "no-store": the copy may be kept, but
                # it has to be revalidated before it is used, and Last-Modified
                # is enough to get a 304 for the unchanged files. A fresh page
                # is a few small requests.
                # The namespace is tools.response_headers, not
                # tools.response.headers: the toolbox registers it under the
                # attribute name it was given, and cherrypy's own name for it is
                # a single identifier. Configured as "response.headers" the
                # tool is simply never found and nothing is sent, which looks
                # exactly like the header not working.
                "tools.response_headers.on": True,
                "tools.response_headers.headers": [("Cache-Control", "no-cache")],
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

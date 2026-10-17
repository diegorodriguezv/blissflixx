#!/usr/bin/env python3
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
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

#: What the machine is doing to itself, or None when it is doing nothing.
#:
#: This is a global rather than anything a browser holds, because it has to reach
#: every open tab and not only the one whose button was pressed. The way it does
#: is the way the restart notice already does it: every tab asks the server for
#: player status once a second, and the answer carries the text. See the status
#: hook in Api.default, and RESTARTING above, which is the same idea for the
#: restart button and has worked there all along.
#:
#: It cannot be told to the browser by the button, because the button has no way
#: to speak to another tab, and it cannot wait for the machine to come back and
#: say it then, because a shutdown never does. Sent once, before the machine
#: stops answering, is the only moment there is.
#:
#: In this process and nowhere else, which is what makes it need no tidying up:
#: a reboot ends by killing the process that holds it, so the text cannot outlive
#: the thing it describes. Every tab then clears it by itself -- after a reboot
#: the fresh status has no text in it, and after a shutdown the poll stops
#: answering and the last status the tab had is the one it keeps.
POWER_MESSAGE = None

#: How long the machine is left alone before it is actually asked to go, in
#: seconds.
#:
#: Long enough for a tab to be told. Tabs poll once a second, so a tab that is
#: polling gets two or three chances to pick the message up before the machine
#: stops answering and there is nothing left to ask. Long enough, and no longer:
#: the point of pressing the button is that the machine goes, and a shutdown
#: never comes back to be waited for.
#:
#: Only the request that was answered with the button waits, in one of CherryPy's
#: ten worker threads. The tab that pressed it is not waiting for that reply --
#: its own message went up when the button was pressed, not when the reply came
#: -- so nothing on screen is late by this.
POWER_GRACE_SECONDS = 3

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

#: What the machine is called while it is doing it. Sent in every status poll
#: while the action is in flight, which is how it reaches tabs other than the one
#: whose button was pressed.
#:
#: These are the same words PowerStore.js puts on screen in the tab that did the
#: pressing, and they have to be: that tab reads them from its own click and
#: every other tab reads them from here, so if the two ever disagreed one tab
#: would say "Rebooting..." while the one beside it said nothing. A test reads
#: both files and compares them.
POWER_MESSAGES = {"reboot": "Rebooting...", "shutdown": "Shutting down..."}

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
            return self._power(fn, "shut down", POWER_ACTIONS["shutdown"])
        elif fn == "reboot":
            return self._power(fn, "reboot", POWER_ACTIONS["reboot"])
        elif fn == "version":
            return self._version()
        else:
            return self._error(404, "API Function '" + str(fn) + "' is not defined")

    def _version(self):
        """
        The build this server is running, as a short commit hash.

        Shown on the settings page, so a browser serving old files can be caught
        rather than argued with. "The reboot bar does not appear" and "this
        browser has been running a build from three days ago" are the same
        silence on the screen, and telling them apart by changing the code and
        asking whether it worked is how an afternoon goes.

        From git, because that is what is deployed and the checkout is what the
        interface is served from. Says so plainly rather than showing nothing
        when git is not there.
        """
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=locations.ROOT_PATH,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if sha.returncode == 0 and sha.stdout.strip():
                return sha.stdout.strip()
        except Exception:
            pass
        return "unknown"

    def _power(self, fn, verb, action):
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

        And then it waits, which is the other half of telling anyone. The text
        above goes out in the status poll, and the poll is what every tab does
        once a second -- so the machine is asked to go only after a poll has had
        time to carry the news. Asking it straight away made the notice a race
        that the machine usually won.

        Nothing on screen is late by the wait. The tab that pressed the button
        says so from the press itself, before this function is called, and waits
        for this reply only to hear that it was refused.
        """
        global POWER_MESSAGE

        # A second press, while the first is still counting down.
        #
        # Refused, and the notice left alone. systemd would answer this one with
        # something about already rebooting, and treating that as the refusal it
        # resembles would take the notice away from every tab a second before the
        # machine does what the first press asked. The first press is not a
        # failure and must not be reported as one.
        #
        # It describes what the machine is doing, not what this button was
        # pressed. Pressing reboot while a shutdown is already under way says
        # the machine is on its way to shutting down, because that is the truth;
        # saying "already trying to reboot" there would name the wrong one of
        # the two and send whoever reads it looking for a reboot that is not
        # coming.
        if POWER_MESSAGE is not None:
            return self._error(
                409,
                "The machine is already on its way: %s This press did nothing."
                % POWER_MESSAGE,
            )

        POWER_MESSAGE = POWER_MESSAGES[fn]
        cherrypy.log("power: %s, in %s seconds" % (POWER_MESSAGE, POWER_GRACE_SECONDS))
        time.sleep(POWER_GRACE_SECONDS)

        try:
            result = subprocess.run(
                ["systemctl", action], capture_output=True, text=True, timeout=30
            )
        except Exception as exc:
            # Refused, so there is nothing to announce: the machine is staying
            # up and every tab is told so by the next poll, which finds the text
            # gone.
            POWER_MESSAGE = None
            return self._error(500, "Could not %s: %s" % (verb, exc))
        if result.returncode == 0:
            # Left set on purpose. The machine is on its way down, and the
            # process that holds this is going with it, so there is nothing to
            # clear it and nothing to clear it for.
            return None
        POWER_MESSAGE = None
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
                if modname == "playr" and fn == "status":
                    # Where a machine that is going away says so. Two ways to be
                    # on the way out and one way to be told: the restart notice
                    # was here first and has been working, and a reboot or a
                    # shutdown now says the same thing by the same road.
                    #
                    # Every tab asks for this once a second and renders Msg in
                    # the bar it already uses for errors, so this is the whole of
                    # how a notice reaches tabs other than the one that pressed
                    # the button. Restart is what keeps their close button hidden:
                    # the text is not the reader's to dismiss, it is the machine
                    # saying what it is about to do.
                    if RESTARTING:
                        ret["Error"] = True
                        ret["Msg"] = "Server Restarting & Updating..."
                        ret["Restart"] = True
                    elif POWER_MESSAGE is not None:
                        ret["Error"] = True
                        ret["Msg"] = POWER_MESSAGE
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


class HtmlDocument:
    """
    The interface, with the build stamped into the document that is served.

    The whole of a bad afternoon: a browser kept a saved copy of index.html
    because the meta tags make this a PWA, and a saved document loads the
    JavaScript it was saved with. So the server could be three weeks ahead and
    the browser would never show it, reloading included -- and the only
    available answer to "I changed it and nothing happened" was to change it
    again. Three rounds of that, answered wrongly three times.

    Two things are done here that could not be done from the page:

    - Every local script and stylesheet gets the build on its URL. A new build
      means new URLs, so a stale copy in any cache is simply not asked for
      again. There is no window where the document is current and the scripts
      under it are not, which is the only state worth worrying about.

    - The build is written into the document itself, so the number on the
      screen comes from the server that served it rather than from JavaScript
      that might be weeks old. A diagnostic that can be stale is not a
      diagnostic.

    Only local URLs are stamped. The three stylesheets and jQuery come from
    CDNs, and appending a query to those would defeat the caching they are
    there for while telling us nothing.
    """

    #: Same-origin URLs only: a relative path, or one rooted at "/". Anything
    #: with a host of its own -- "//example.com/x" or "https://example.com/x" --
    #: is somebody else's cache, and appending a query there would defeat the
    #: caching it is there for while telling us nothing about this build.
    _local_asset = re.compile(
        r'(<(?:script|link)\b[^>]*?\s(?:src|href)=")(?!//|https?:)([^"?]+)(")'
    )

    @staticmethod
    def _server_build():
        """
        What is checked out here, which is what is being served.

        Says "unknown" rather than raising: a machine with no git checkout
        still has an interface, and a version stamp is not worth a 500.
        """
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=locations.ROOT_PATH,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if sha.returncode == 0 and sha.stdout.strip():
                return sha.stdout.strip()
        except Exception:
            pass
        return "unknown"

    @cherrypy.expose
    def index(self, *args, **kwargs):
        build = self._server_build()
        path = os.path.join(locations.HTML_PATH, "index.html")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                page = handle.read()
        except OSError:
            raise cherrypy.HTTPError(500, "index.html could not be read")

        def stamp(match):
            url = match.group(2)
            if url.startswith("?") or "v=" in url:
                return match.group(0)
            return match.group(1) + url + "?v=" + build + match.group(3)

        page = self._local_asset.sub(stamp, page)
        # Last, so nothing after it can be stamped, and so the number on the
        # screen is the one this document was served with.
        #
        # The build is also handed to the page as a variable, because one set
        # of files cannot be stamped from here: pages/*.html is fetched by riot
        # at runtime, once per route, and has to be asked for by name later. The
        # document is the only place that knows the build at load time, so it
        # passes it on and app.html puts it on those requests too.
        page = page.replace(
            "</head>",
            '<meta name="build" content="%s">\n'
            "    <script>window.BF_BUILD = '%s';</script>\n"
            "  </head>" % (build, build),
            1,
        )
        cherrypy.response.headers["Content-Type"] = "text/html;charset=utf-8"
        # Set here rather than left to the "/": this is the way in, and the way
        # in is what gets kept. Everything else under "/" -- the scripts, the
        # stylesheets -- is no-cache, which is enough once the document that
        # names them is never stored. The build on their URLs covers the rest.
        cherrypy.response.headers["Cache-Control"] = "no-store, must-revalidate"
        return page

    @cherrypy.expose
    def default(self, *vpath, **kwargs):
        """
        "/index.html", which is what a browser asks for when it has a saved copy
        of the document.

        Served here rather than by staticdir, so that the way back in cannot
        skip the build stamping and hand back a document that names scripts from
        whenever it was saved. A browser holding a week-old copy of this exact
        path is the case this whole change exists for.

        staticdir has already had its turn with everything else by the time
        this is reached, so what arrives here is a path with no file behind it.
        """
        if vpath == ("index.html",):
            return self.index()
        raise cherrypy.HTTPError(404)


    @staticmethod
    def _server_build():
        try:
            sha = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=locations.ROOT_PATH,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if sha.returncode == 0 and sha.stdout.strip():
                return sha.stdout.strip()
        except Exception:
            pass
        return "unknown"


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", help="Run as daemon process", action="store_true")
    parser.add_argument("--port", type=int, help="Listen port (default 6969)")
    return parser.parse_args(argv)


def mount_trees():
    cherrypy.tree.mount(Api(), "/api")
    cherrypy.tree.mount(
        HtmlDocument(),
        "/",
        config={
            "/": {
                "tools.staticdir.on": True,
                "tools.staticdir.dir": locations.HTML_PATH,
                # No index file here: the document is built by Html.index()
                # below so the build can be stamped into it. staticdir's own
                # index would serve the file off disk, unstamped, and "/" and
                # "/index.html" would then be two different documents.
                "tools.staticdir.index": "",
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
            # The document itself is never cached, not even revalidated.
            #
            # no-cache on everything else is enough as long as the browser asks.
            # It does not always: this app is a PWA by virtue of its meta tags,
            # so browsers install it and keep the document, and a kept document
            # is one that loads the JavaScript it was saved with. A browser
            # holding last week's index.html asks for last week's scripts and
            # never sees anything new, however many times it is reloaded --
            # which is what three rounds of "I changed it and nothing happened"
            # turned out to be.
            #
            # Specific paths win over "/" in cherrypy's config, so this only
            # applies to the document.
            "/index.html": {
                # Static turned off for this one path, so it falls through to
                # HtmlDocument.default() below and is built like "/" is. With
                # staticdir handling it, the browser gets the file off disk and
                # none of the stamping -- which is precisely the request a
                # browser holding a saved copy makes.
                "tools.staticdir.on": False,
                "tools.response_headers.on": True,
                "tools.response_headers.headers": [
                    ("Cache-Control", "no-store, must-revalidate")
                ],
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

#!/usr/bin/env bash

# Install system dependencies for blissflixx.

# How long each part took, and how long the whole thing did.
#
# Printed as it goes rather than only at the end, because this script runs for
# the best part of an hour and the interesting failure is not the one that
# stops it: an apt repository that fails partway leaves a machine that looks
# installed and is not, and the only way to tell is which stage is missing
# from the log. Stage timings make that visible without a timestamp on every
# file.
SECONDS=0
# Empty, not 0. SECONDS is also 0 when the script starts, so a zero sentinel
# collided with it: the first stage set STAGE_START to 0, the "has a stage
# started" test kept reading 0 as "no", and that stage was never reported at
# all. Running a single stage printed nothing and looked like the work had been
# skipped. Tested with -n rather than arithmetic, because a string is what says
# "unset" without having to pick a number nothing legitimate can be.
STAGE_START=""
STAGE_NAME=""

stage() {
    local now=$SECONDS
    # The name printed here is the stage that just finished, beside its own
    # duration.
    #
    # It used to be the name of the stage starting, beside the duration of the
    # one before it, so every line named one stage and timed another. Read
    # plainly it said something false: installing the reboot and shutdown
    # buttons reported "the reboot and shutdown buttons 0s", which reads as
    # those buttons having taken no time, when it was the stage before them
    # that had. A line that misreports its own subject is worse than none.
    #
    # So the previous name is kept and printed when the next stage begins. The
    # last stage has no next stage, so it is flushed explicitly -- otherwise
    # running a single stage prints nothing at all.
    if [ -n "$STAGE_START" ]; then
        printf '  %-28s %s\n' "$STAGE_NAME" "$(elapsed $((now - STAGE_START)))"
    fi
    STAGE_START=$now
    STAGE_NAME=$1
}

stage_flush() {
    if [ -n "$STAGE_START" ]; then
        printf '  %-28s %s\n' "$STAGE_NAME" "$(elapsed $((SECONDS - STAGE_START)))"
        STAGE_START=""
        STAGE_NAME=""
    fi
}

# Seconds as 1h02m03s or 45s, whichever is readable.
elapsed() {
    local s=$1
    if [ $s -ge 3600 ]; then
        printf '%dh%02dm%02ds' $((s / 3600)) $(((s % 3600) / 60)) $((s % 60))
    elif [ $s -ge 60 ]; then
        printf '%dm%02ds' $((s / 60)) $((s % 60))
    else
        printf '%ds' "$s"
    fi
}

# Make sure only root can run our script
if [ "$(id -u)" != "0" ]; then
    echo "This script must be run using sudo" 1>&1
    exit 1
fi

# The person this is being installed for.
#
# logname asks the terminal, and there is not always one: run through
# systemd-run or any other detached shell it prints nothing, and an empty name
# was written into the unit as "User=" -- which means root -- while
# "sudo -u ./configure_py.sh" failed with "unknown user". SUDO_USER is
# whatever sudo was told to become, which is exactly the person who typed the
# password, and it does not need a terminal.
BF_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"

if [[ $@ == *-only* ]] ; then
    echo running only optional arguments
else
    echo ""
    echo "============================================================"
    echo ""
    echo "Installing system dependencies... (This will take a while)"
    echo "Expect about 45 minutes on a Raspberry Pi 3."
    echo ""
    echo "============================================================"

    echo "  Updating package lists"
    apt update
    stage "package lists"

    # A JavaScript runtime, for yt-dlp's YouTube extraction.
    #
    # Not the distribution's. yt-dlp refuses node below version 22 and Raspberry
    # Pi OS ships 20. This is the pinned LTS release, unpacked into the checkout
    # instead of installed system-wide: swapping the system node would move the
    # runtime the distribution's own packages were installed with, which is a
    # change to the machine rather than to this project. lib/player/nodejs.py is
    # the one place that knows the path.
    #
    # Checked against the release's own SHASUMS256.txt. A download that arrives
    # damaged is a mystery later ("it worked on the other Pi"), and a runtime
    # that moved on its own is the sort of change everything else here is pinned
    # against.
    NODE_VERSION="v22.23.3"
    case "$(uname -m)" in
        armv7l) NODE_ARCH="linux-armv7l" ;;
        aarch64|arm64) NODE_ARCH="linux-arm64" ;;
        x86_64|amd64) NODE_ARCH="linux-x64" ;;
        *) NODE_ARCH="" ;;
    esac
    NODE_TARBALL="node-$NODE_VERSION-$NODE_ARCH.tar.xz"
    if [ -n "$NODE_ARCH" ] &&
       curl -fsS --max-time 300 -o "/tmp/$NODE_TARBALL" \
           "https://nodejs.org/dist/$NODE_VERSION/$NODE_TARBALL" &&
       curl -fsS --max-time 120 -o /tmp/SHASUMS256.txt \
           "https://nodejs.org/dist/$NODE_VERSION/SHASUMS256.txt" &&
       ( cd /tmp && grep " $NODE_TARBALL\$" SHASUMS256.txt | sha256sum -c - ); then
        mkdir -p data/node
        tar -xJf "/tmp/$NODE_TARBALL" -C data/node --strip-components=1
        # Only the runtime is used: yt-dlp runs `node` for YouTube's JavaScript
        # challenges. The tarball also carries 65MB of C++ headers for building
        # native addons and 18MB of npm and corepack; nothing here runs npm, so
        # both are removed rather than left as the largest thing in the checkout.
        rm -rf data/node/include data/node/lib/node_modules \
               data/node/CHANGELOG.md data/node/README.md
        echo "  node $NODE_VERSION in $(pwd)/data/node ($(du -sh data/node | cut -f1))"
    else
        echo "  node $NODE_VERSION not installed; yt-dlp will fall back to quickjs"
    fi
    rm -f "/tmp/$NODE_TARBALL" /tmp/SHASUMS256.txt
    stage "node runtime"

    # A second runtime yt-dlp accepts, so that a failed download above costs
    # speed and not YouTube. Its build clears the minimum yt-dlp asks for
    # (2023-12-09); yt-dlp ignores a runtime it cannot use rather than failing.
    apt -y install quickjs
    stage "quickjs"

    # Install default fonts for subtitles
    apt install fonts-freefont-ttf
    stage "subtitle fonts"

    # The player. VLC decodes H.264 in hardware, sends audio to HDMI and burns
    # in subtitles without an expensive compositing path.
    if [[ $@ != *-no-players* ]] ; then
        apt -y install vlc
        # A box installed before this carried two more backends. They are gone
        # from the code, so their packages are removed rather than left to
        # occupy 40MB and be reported as broken by check_players.
        apt-get -y purge mpv gstreamer1.0-tools gstreamer1.0-plugins-base \
            gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
            gstreamer1.0-libav > /dev/null 2>&1 || true
        stage "vlc"
    fi

    # ffmpeg, which yt-dlp uses where a stream has to be remuxed or a download
    # reassembled. It is named rather than left to arrive as somebody's
    # dependency, so it cannot disappear with a package that is removed.
    apt -y install ffmpeg

    # Older installs pulled in the distribution's yt-dlp as a recommended
    # dependency of mpv. It is not used -- the virtualenv's is unpinned and
    # tracks the sites it extracts from -- so it is removed rather than left on
    # PATH, stale.
    apt-get -y purge yt-dlp > /dev/null 2>&1 || true
    stage "ffmpeg"

    # Install version control software for updates and to install other packages like
    # yt-dlp. It is probably already installed, but it is possible that the user
    # got blissflixx other way, like downloading the source as a zip file.
    apt -y install git
    stage "git"

    # Install python package manager
    apt -y install python3-pip
    
    # Install python virtual environment to keep blissflixx dependencies isolated
    apt -y install python3-venv
    
    # Install XML parser for screen scaping websites
    apt -y install libxml2-dev
    
    # Install XSLT package for screen scaping websites
    apt -y install libxslt1-dev

    # libtorrent, for the torrent source that fetches only the pieces a play
    # reads. It is a system package rather than a pip one, so it lives in the
    # system python and bin/bt_serve.py is run with that interpreter; see
    # lib/player/ltproc.py. The stage reports its absence in one line, with this
    # command, rather than failing obscurely.
    apt -y install python3-libtorrent
    stage "python and build libraries"
    
    # Binding port 80 needs no help here: the service unit asks for
    # CAP_NET_BIND_SERVICE itself, through AmbientCapabilities, which scopes the
    # capability to the service rather than to the interpreter.
    #
    # This used to be a file capability set on python3, which is granted to
    # every Python program on the machine and survives uninstall. It was also
    # unreliable -- on a current Raspberry Pi OS the xattr was not visible on
    # any binary that was running, so nothing could say what was actually
    # allowing the bind.

    # Torrents are fetched by libtorrent, in the system python
    # (python3-libtorrent, installed above), so there is no node torrent engine
    # to install, patch or keep up to date. A box upgraded from a version that
    # had one still carries it -- 43MB of npm tree and its launcher -- so it is
    # removed here rather than left to rot.
    rm -rf /usr/local/lib/node_modules/peerflix
    rm -f /usr/local/bin/peerflix
    stage "peerflix removed"
    
    # Configure python dependencies as the person the install is for: they own
    # the checkout, and a virtualenv built by root is not theirs to run.
    sudo -u "$BF_USER" ./configure_py.sh
    stage "python virtual environment"

    # Report what is actually usable. Installing a player is not the same as
    # being able to run it: the ALSA and video output names in its defaults are
    # specific to one model. The checks come from those defaults, so they stay
    # correct as they change.
    echo ""
    echo "============================================================"
    echo ""
    sudo -u $user ./virtualenv/bin/python ./bin/check_players.py || true
    echo ""
    echo "============================================================"
    echo ""
    stage "checking what is usable"
    echo "Dependencies installed in $(elapsed $SECONDS)."
    echo ""
fi

# Check for optional script arguments

if [[ $@ == *-boot* ]] ; then
    systemctl stop blissflixx.service
    systemctl disable blissflixx.service
  cat << _contents > /etc/systemd/system/blissflixx.service
[Unit]
Description=Blissflixx service
After=network-online.target

[Service]
Type=simple
Restart=always
User=${BF_USER}
WorkingDirectory=$(pwd)
ExecStart='$(pwd)/run.sh'

# To bind port 80 without root. Scoped to this service rather than granted to
# the interpreter: a file capability on python3 belongs to every Python program
# on the machine, not just this one, and outlives the install that put it there.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

[Install]
WantedBy=default.target
_contents
    # systemd execs this directly; a checkout without the bit fails with
    # status 126 and a service that will not start.
    chmod +x run.sh
    # The checks above run the application as root, which can leave data/ owned
    # by root; the service then cannot create its own directories and fails at
    # startup with a PermissionError on data/playlists.
    chown -R "$BF_USER":"$BF_USER" "$(pwd)"
    systemctl daemon-reload
    systemctl enable blissflixx.service
    # Started, not just enabled. The unit was stopped at the top of this block,
    # and enabling only says "start it next time the machine boots" -- so
    # running this took the server down and left it there, and the interface
    # answered "It looks like the server is not running" to everything until the
    # next reboot. Found by installing the reboot button and then pressing it:
    # the click reached a server that this script had already taken away.
    systemctl restart blissflixx.service
    stage "the startup service"

    # Who ran this. SUDO_USER is what sudo sets it to, and it is the only one
    # of these that is right when configure.sh is run over ssh with no
    # terminal, which is how it usually is. A wrong name here means a grant
    # nobody matches, and a check below that passes for the wrong person.
    # Defined before both uses, which are far apart in this block.
    BF_USER="${SUDO_USER:-$(logname 2>/dev/null || whoami)}"

    # The interface has a REBOOT button, and it could not work.
    #
    # Not because of permissions, which is where this went looking first. The
    # server runs from this service, and a service has no login session, so its
    # loginuid is unset:
    #
    #     $ cat /proc/self/loginuid
    #     4294967295
    #
    # which is -1. libaudit needs a loginuid to record which user invoked a
    # command, finds none, cannot open the audit log, and sudo gives up before
    # it has read anything at all:
    #
    #     sudo: error initializing audit plugin sudoers_audit
    #
    # `sudo -l`, which only reads the policy and asks no question, fails
    # identically. So no sudo grant can fix it, however carefully written --
    # sudo never reaches the point of reading one. The same command over ssh,
    # where loginuid is 1000, works every time, which is what makes this look
    # like a permissions problem from a terminal and not one from a service.
    #
    # polkit is the mechanism built for this. It authorises a named action for a
    # subject, and "inactive" is its word for a process with no session, which
    # is exactly what this service is. So the rule below allows the two power
    # actions, and nothing else.
    #
    # Numbered 99 so it sorts after the distribution's own rules, which is the
    # order polkit applies them in.
  cat << _contents > /etc/polkit-1/rules.d/99-blissflixx-power.rules
// Written by configure.sh. The reboot and shutdown buttons.
//
// This is JavaScript, not XML. polkit compiles its rules as scripts, and on
// Debian bookworm that dialect is JavaScript -- the distribution's own rules
// are polkit.addRule(function(action, subject) {...}). An XML rule goes to the
// same compiler and is refused:
//
//     Error compiling script .../99-blissflixx-power.rules:
//         SyntaxError: parse error (line 1)
//
// "SyntaxError" and "parse error" are the JavaScript compiler talking, and the
// line is whichever line the JavaScript first went wrong on -- here line 1,
// which held <policyconfig>. Written as XML twice before that message was read
// as what it was: a syntax error in a script, not a malformed document. The
// XML looked perfectly valid, and it was.
//
// Narrow on purpose: two actions, and only for the account BlissFlixx runs as.
// The rule applies whether or not there is a session, and the sessionless case
// is the one that matters -- the server is a service and has none.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.login1.reboot" ||
        action.id == "org.freedesktop.login1.reboot-multiple" ||
        action.id == "org.freedesktop.login1.power-off" ||
        action.id == "org.freedesktop.login1.power-off-multiple") {
        if (subject.user == "$BF_USER") {
            return polkit.Result.YES;
        }
    }
});
_contents
    chmod 644 /etc/polkit-1/rules.d/99-blissflixx-power.rules

    # Reloaded and then checked, so a rule that does not take effect is
    # reported by the install that wrote it rather than discovered later by
    # pressing the button.
    #
    # With --process naming a process the account owns, because pkcheck with no
    # subject at all does not say "not authorised" -- it says "Subject not
    # specified" and exits 126, which is a question that was never asked. An
    # earlier version of this check ran it that way and reported a warning on an
    # install that worked, which is worse than no check: it teaches you to
    # distrust the one thing that is telling you the truth.
    #
    # Checked twice. The second runs it under setsid, so the process has no
    # session -- which is the BlissFlixx server, and the only case this rule
    # exists for. polkit reports no error when a rule compiles but does not
    # cover a sessionless subject, it just refuses, so without that second
    # check the install would pass and the button would still fail.
    systemctl reload polkit
    BF_ACTION=org.freedesktop.login1.reboot
    if ! sudo -u "$BF_USER" sh -c "pkcheck --action-id $BF_ACTION --process \$\$" > /dev/null 2>&1 ; then
        echo "  WARNING: the reboot rule is not taking effect for $BF_USER." 1>&2
        echo "  polkit's own log says why:" 1>&2
        journalctl -u polkit --since "1 min ago" --no-pager 2>/dev/null | \
            grep -i "99-blissflixx" | tail -2 | sed 's/^/    /' 1>&2
    elif ! sudo -u "$BF_USER" setsid sh -c "pkcheck --action-id $BF_ACTION --process \$\$" > /dev/null 2>&1 ; then
        echo "  WARNING: the rule works from a terminal but NOT for a process" 1>&2
        echo "  with no session, which is the BlissFlixx server, so the button" 1>&2
        echo "  will still fail. polkit reports no error for this; it just" 1>&2
        echo "  refuses." 1>&2
    fi
    stage "the reboot and shutdown buttons"

    # The restart helper stays a sudo grant, because it runs from a terminal
    # where sudo works and is not involved in the above. It is what a manual
    # restart after an update goes through, and nothing created it on a clean
    # install -- the grant named a file that was not there.
  cat << _contents > /usr/local/sbin/blissflixx-restart
#!/bin/sh
# Restart BlissFlixx. Unit is hardcoded; arguments ignored.
exec /usr/bin/systemctl restart blissflixx.service
_contents
    chmod 755 /usr/local/sbin/blissflixx-restart

    # Removed, not left: an earlier attempt at the reboot and shutdown buttons
    # went through sudo, with a grant and a script per action. Neither can work
    # -- sudo cannot run from the service that would use them -- and both files
    # are dead on disk afterwards. Nothing on a clean install creates them, so
    # an install that leaves them behind has quietly diverged from one.
    rm -f /usr/local/sbin/blissflixx-reboot /usr/local/sbin/blissflixx-halt

    # Who ran this, for the sudo grant below. Set at the top of this block.
    cat << _contents > /etc/sudoers.d/blissflixx
# Written by configure.sh. The script takes no arguments, which is why the empty
# pair of quotes at the end: that is how a sudoers command with no arguments is
# spelled, and passing an argument -- even an empty one -- makes sudo ask for a
# password again.
#
# Reboot and halt are deliberately absent. They are a polkit rule instead, in
# 99-blissflixx-power.rules: sudo cannot run from the service that would use
# them, whatever this file says.
$BF_USER ALL=(root) NOPASSWD: /usr/local/sbin/blissflixx-restart ""
_contents
    chmod 440 /etc/sudoers.d/blissflixx

    # Checked before going any further, and removed if it is wrong: a sudoers
    # file that does not parse stops sudo working for everyone on the machine,
    # and the symptom of that is not finding out now. By its full path, since a
    # non-interactive shell may not have /usr/sbin on its PATH -- and the check
    # must not be the thing that skips itself when it cannot find visudo.
    if [ -x /usr/sbin/visudo ] ; then
        if ! /usr/sbin/visudo -cf /etc/sudoers.d/blissflixx > /dev/null 2>&1 ; then
            rm -f /etc/sudoers.d/blissflixx
            echo "The sudo grant did not parse, so it was removed." 1>&2
        fi
    fi
    stage_flush
fi

if [[ $@ == *-blank* ]] ; then
    systemctl stop blankscreen.service
    systemctl disable blankscreen.service
    cat << _contents > /etc/systemd/system/blankscreen.service
[Unit]
Description=Blank screen after boot

[Service]
Type=simple
User=${BF_USER}
ExecStart=$(pwd)/blank_screen.sh
ExecStartPre=/bin/sleep 15

[Install]
WantedBy=default.target
_contents
    systemctl daemon-reload
    systemctl enable blankscreen.service
    stage "the blank screen service"
fi

if [[ $@ == *-rejoin* ]] ; then
    systemctl stop rejoin.service
    systemctl disable rejoin.service
  cat << _contents > /etc/systemd/system/rejoin.service
[Unit]
Description=Rejoin, a wifi connection repair service
After=network-online.target

[Service]
Type=simple
Restart=always
User=${BF_USER}
WorkingDirectory=$(pwd)
ExecStart='$(pwd)/rejoin.sh'

[Install]
WantedBy=default.target
_contents
    systemctl daemon-reload
    systemctl --now enable rejoin.service
    stage "the wifi repair service"
fi

if [[ $@ == *-uninstall* ]] ; then
    systemctl stop blankscreen.service
    systemctl disable blankscreen.service
    rm /etc/systemd/system/blankscreen.service
    systemctl stop blissflixx.service
    systemctl disable blissflixx.service
    rm /etc/systemd/system/blissflixx.service
    systemctl stop rejoin.service
    systemctl disable rejoin.service
    rm /etc/systemd/system/rejoin.service
    systemctl daemon-reload
fi

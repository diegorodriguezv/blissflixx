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
STAGE_START=0

stage() {
    local now=$SECONDS
    if [ $STAGE_START -ne 0 ]; then
        printf '  %-28s %s\n' "$1" "$(elapsed $((now - STAGE_START)))"
    fi
    STAGE_START=$now
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

    # This includes nodejs
    apt -y install npm
    stage "npm (and nodejs)"

    # Install default fonts for subtitles
    apt install fonts-freefont-ttf
    stage "subtitle fonts"

    # Player backends.
    #
    # VLC is the default backend.
    if [[ $@ != *-no-players* ]] ; then
        apt -y install vlc
        apt -y install mpv
        stage "mpv"
        # GStreamer for the most CPU-efficient pipeline. The element set depends
        # on which plugin packages are installed, so bin/check_players.py
        # verifies afterwards what actually arrived.
        apt -y install gstreamer1.0-tools \
                       gstreamer1.0-plugins-base \
                       gstreamer1.0-plugins-good \
                       gstreamer1.0-plugins-bad \
                       gstreamer1.0-libav
        stage "vlc and gstreamer"
    fi

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

    # Install peerflix globally
    npm install -g peerflix
    stage "peerflix"
    
    # The user that started the command (since sudo was used, root is the active user)
    user=$(logname)
    
    # Configure python dependencies as a normal user
    sudo -u $user ./configure_py.sh
    stage "python virtual environment"

    # Report what is actually usable. Installing a player is not the same as
    # being able to run it: stock GStreamer has no v4l2h264dec, so the GStreamer
    # pipeline needs video_decoder changed to avdec_h264. The checks come from
    # the backends' own defaults, so they stay correct as backends change.
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
User=$(logname)
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
    systemctl daemon-reload
    systemctl enable blissflixx.service
    stage "the startup service"
fi

if [[ $@ == *-blank* ]] ; then
    systemctl stop blankscreen.service
    systemctl disable blankscreen.service
    cat << _contents > /etc/systemd/system/blankscreen.service
[Unit]
Description=Blank screen after boot

[Service]
Type=simple
User=$(logname)
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
User=$(logname)
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

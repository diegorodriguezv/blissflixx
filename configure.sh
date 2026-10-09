#!/usr/bin/env bash

# Install system dependencies for blissflixx.

# Make sure only root can run our script
if [ "$(id -u)" != "0" ]; then
    echo "This script must be run using sudo" 1>&2
    exit 1
fi

if [[ $@ == *-only* ]] ; then
    echo running only optional arguments
else
    echo ""
    echo "============================================================"
    echo ""
    echo "Installing system dependencies... (This will take a while)"
    echo ""
    echo "============================================================"
    
    # Update software repositories 
    apt update
    
    # This includes nodejs
    apt -y install npm
    
    # Install default fonts for subtitles
    apt install fonts-freefont-ttf

    # Player backends.
    #
    # VLC is the default backend. omxplayer has no package on current Raspberry
    # Pi OS, so installing it fails; it is attempted because the legacy
    # backends still work on the old OS, but a failure here is not fatal and the
    # rest of the install continues.
    if [[ $@ != *-no-players* ]] ; then
        apt -y install vlc
        apt -y install mpv
        # GStreamer for the most CPU-efficient pipeline. The element set depends
        # on which plugin packages are installed, so bin/check_players.py
        # verifies afterwards what actually arrived.
        apt -y install gstreamer1.0-tools \
                       gstreamer1.0-plugins-base \
                       gstreamer1.0-plugins-good \
                       gstreamer1.0-plugins-bad \
                       gstreamer1.0-libav

        # Best effort: absent on current Raspberry Pi OS.
        apt -y install omxplayer || \
            echo "omxplayer is not available for this OS, which is expected."
    fi

    # Install version control software for updates and to install other packages like
    # yt-dlp. It is probably already installed, but it is possible that the user
    # got blissflixx other way, like downloading the source as a zip file.
    apt -y install git
    
    # Install python package manager
    apt -y install python3-pip
    
    # Install python virtual environment to keep blissflixx dependencies isolated
    apt -y install python3-venv
    
    # Install XML parser for screen scaping websites
    apt -y install libxml2-dev
    
    # Install XSLT package for screen scaping websites
    apt -y install libxslt1-dev
    
    # So server can run on port 80 without sudo
    setcap 'cap_net_bind_service=+ep' "$(readlink -f "$(which python3)")"
    
    # Install peerflix globally
    npm install -g peerflix
    
    # The user that started the command (since sudo was used, root is the active user)
    user=$(logname)
    
    # Configure python dependencies as a normal user
    sudo -u $user ./configure_py.sh

    # Report what is actually usable. Installing a player is not the same as
    # being able to run it: omxplayer has no package on current Raspberry Pi OS,
    # and stock GStreamer has no v4l2h264dec, so the GStreamer pipeline needs
    # video_decoder changed to avdec_h264. The checks come from the backends'
    # own defaults, so they stay correct as backends change.
    echo ""
    echo "============================================================"
    echo ""
    sudo -u $user ./virtualenv/bin/python ./bin/check_players.py || true
    echo ""
    echo "============================================================"
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

[Install]
WantedBy=default.target
_contents
    systemctl daemon-reload
    systemctl enable blissflixx.service
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

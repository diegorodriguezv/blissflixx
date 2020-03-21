#!/bin/bash

# Make sure only root can run our script
if [ "$(id -u)" != "0" ]; then
   echo "This script must be run using sudo" 1>&2
   exit 1
fi

echo ""
echo "============================================================"
echo ""
echo "Installing necessary dependencies... (This will take a while)"
echo ""
echo "============================================================"

# Update to latest packages
apt-get update

# Fix broken packages
apt-get -y -f install

# Remove old version of node
apt-get -y remove nodejs-legacy

# Install latest node.js
apt-get -y install node

# Install npm 
apt-get -y install npm

# Install latest omxplayer
apt-get -y install omxplayer

# Install peerflix
npm install -g peerflix

# Install GIT
apt-get -y install git

# Install libav for Jessie
apt-get -y install libav-tools

# Install rtmpdump
apt-get -y install rtmpdump

# See https://github.com/blissland/blissflixx/issues/31
apt-get -y install gcc

# install python3
apt-get -y install python3

# Install python pip
apt-get -y install python3-pip

# Install python build tools
apt-get -y install python3-dev

# Install requirements for cherrypy that run on python 3.4
pip3 install more-itertools==7.2.0 tempora==1.14.1 jaraco.functools==2.0

# Install CherryPy
pip3 install CherryPy==17.4.2

# Install subprocess32 module
pip3 install subprocess32

# Install requests module
pip3 install requests

# Install XML parser
apt-get -y install libxml2-dev

# Install XSLT package
apt-get -y install libxslt1-dev

# Install pycrypto
apt-get install python-crypto

# Install lxml module
pip3 install lxml==4.3.5

# Install cssselect module
pip3 install cssselect

# Required for setcap
apt-get -y install libcap2-bin

# So server can run on port 80 without sudo
setcap 'cap_net_bind_service=+ep' $(readlink -f $(which python3))

# Install bonjour for raspberrypi.local 
apt-get -y install libnss-mdns

# Install phantomjs (required by youtube-dl for some extractors)
apt-get -y install libfontconfig1 libfreetype6
wget https://github.com/fg2it/phantomjs-on-raspberry/releases/download/v2.1.1-wheezy-jessie-armv6/phantomjs_2.1.1_armhf.deb
sudo dpkg -i phantomjs_2.1.1_armhf.deb
rm phantomjs_2.1.1_armhf.deb

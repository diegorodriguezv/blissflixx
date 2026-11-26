#!/usr/bin/env bash

# Run blissflixx using it's virtual environment. This is a helper method 
# for the blissflixx service that runs forever.

echo $(pwd)
. ./virtualenv/bin/activate
echo running blissflixx using 
echo python3=$(which python3) 
echo pip3=$(which pip3)
echo user=$(whoami)
echo pwd=$(pwd)
# The editable install puts the entry point here. A checkout that pulled this
# change before re-running configure_py.sh has the tree but not the command,
# so make the install once instead of failing to start.
if [ ! -x ./virtualenv/bin/blissflixx ]; then
    ./virtualenv/bin/pip install --no-deps -e . || exit 1
fi
./virtualenv/bin/blissflixx --port 80 

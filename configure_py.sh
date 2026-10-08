#!/usr/bin/env bash

# Install python dependencies into their own virtual environment.
# This is done to reduce conflicts with system defaults or other
# installed applications.

# Make sure script is not run by root
if [ "$(id -u)" == "0" ]; then
   echo "This script must NOT be run using sudo" 1>&2
   exit 1
fi

echo ""
echo "============================================================"
echo ""
echo "Installing python dependencies... "
echo ""
echo "============================================================"

# Create virtual environment
if [ ! -d virtualenv ]; then
    python3 -m venv virtualenv
fi

# Activate virtual environment
. virtualenv/bin/activate

# Runtime dependencies. Kept in requirements.txt so they are declared in one
# place rather than as a list of pip invocations here.
echo "Installing runtime dependencies..."
pip3 install --upgrade pip
pip3 install -r requirements.txt

# Development dependencies (formatters, test runner). Not needed to run the
# server. Pass --dev to include them, e.g.
#   ./configure_py.sh --dev
if [ "$1" == "--dev" ]; then
    echo "Installing development dependencies..."
    pip3 install -r requirements-dev.txt
    echo ""
    echo "Run the test suite with:  virtualenv/bin/pytest"
    echo "Check formatting with:    virtualenv/bin/black --check lib chls blissflixx.py"
    echo "                         virtualenv/bin/isort --check-only lib chls blissflixx.py"
fi

echo ""
echo "Done."
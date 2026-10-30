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
# lxml only: its wheel is what makes the libxml2-dev/libxslt1-dev build
# dependencies unnecessary. If a version ever ships without an armv7l/arm64
# wheel, failing here says so plainly rather than half-building.
pip3 install --only-binary lxml -r requirements.txt

# What yt-dlp arrived as, said plainly.
#
# It is the one package here that is deliberately not pinned, because it
# follows the sites it extracts from rather than this project. A number is
# printed because "did the install work" and "which version is it now" are
# different questions, and only one of them is answered by the absence of an
# error.
echo ""
echo "yt-dlp version installed:"
python -m yt_dlp --version

# Channels that are separate projects.
#
# plugins/ is not in this repository, so a fresh checkout has none of them, and
# the channel manager only lists what it finds. plugins.txt is the list, and
# this is the step that makes a fresh install the same install as an
# established one. Anything already there is left alone: it may have been
# changed, and an update pulls those rather than reinstalling them.
#
# A clone that fails is reported and does not stop the script. One missing
# channel is not a reason to leave the machine half installed with nothing
# saying which half.
if [ -f plugins.txt ]; then
  # "|| [ -n \"$name\" ]" is not noise. read exits non-zero on a final line with
  # no newline after it, and without the second test that line is dropped
  # silently -- which is how this installed nothing at all the first time, on a
  # manifest whose last entry happened to be the only one with a plugin in it.
  while read -r name url || [ -n "$name" ]; do
    case "$name" in
      ""|\#*) continue ;;
    esac
    if [ -d "plugins/$name" ]; then
      echo "  plugins/$name is already there"
    elif git clone "$url" "plugins/$name"; then
      echo "  installed plugins/$name"
    else
      echo "  WARNING: plugins/$name did not clone. That channel will be missing." 1>&2
    fi
  done < plugins.txt
fi

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
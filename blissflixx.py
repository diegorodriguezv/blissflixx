#!/usr/bin/env python3
"""Compatibility entry point for ./run.sh and existing service units.

The server moved from this file to :mod:`blissflixx.server` when the package
was renamed from ``lib``. An installation that has not re-run
``configure.sh`` still starts ``./blissflixx.py``, so this shim stays for one
release: it imports the real entry point and calls it. Remove it once every
installation starts the console script that the editable install generates.
"""

from blissflixx.server import main

if __name__ == "__main__":
    main()

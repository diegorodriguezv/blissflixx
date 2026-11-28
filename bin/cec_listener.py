#!/usr/bin/env python3
"""The CEC listener at the path the installed unit still starts.

`configure.sh` wrote `/usr/bin/python3 <checkout>/bin/cec_listener.py` into
`blissflixx-cec.service`. An installation that has not re-run the installer
still has that unit, so this shim stays for one release: it starts
:mod:`blissflixx.cec` from the checkout under whatever interpreter the unit
names. Remove it when every unit points at `virtualenv/bin/blissflixx-cec`.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blissflixx.cec import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

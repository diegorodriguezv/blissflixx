#!/bin/sh
# Run bt_serve.py, and start it again once if it was killed by a signal.
#
# libtorrent has crashed this process twice on the 32-bit boxes, both times
# after "pieces armed" and before the port was open: a SIGSEGV in the C++ under
# the bindings, intermittent (twenty direct runs without one), and invisible to
# Python. It is the only failure this project has that nobody can act on, and it
# happens at the one moment when repeating the work is safe: no player has been
# given the URL yet, so a second attempt costs a few seconds and nothing else.
#
# Only a signal is retried. The process exits 2, 3 or 4 when it has something to
# say -- no port, no metadata, a disk error -- and repeating those would be
# repeating a known failure. Exit 0 is a play that ended the way it should.
#
# If the crash comes later, during a play, the second attempt binds the same
# port and serves the same file from the same buffer; the player's next read
# may recover, and if it does not, the error is what it would have been.

here=$(dirname "$0")
python=${BT_PYTHON:-/usr/bin/python3}
serve=${BT_SERVE:-$here/bt_serve.py}

run() {
    "$python" "$serve" "$@"
}

run "$@"
status=$?

if [ "$status" -ge 128 ]; then
    echo "bf-bt: killed by signal $((status - 128)); starting it again" >&2
    run "$@"
    status=$?
fi

exit "$status"

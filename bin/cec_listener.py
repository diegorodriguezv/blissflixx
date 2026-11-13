#!/usr/bin/env python3
"""
Play the film from the TV remote, over HDMI-CEC.

The TV forwards the remote's play, pause, arrows, select, back and colour
keys to a CEC playback device as USER_CONTROL messages, and the kernel turns
them into key events on the "vc4-hdmi" input device. This process claims the
Pi as a playback device, reads those key events and sends the matching player
action to the local server, the same way the interface does.

Only what the TV forwards is mapped. Numbers, volume, mute, home and guide are
kept by the TV and never reach this process.

Off unless data/settings/cec.json says {"enabled": true}. When it is off the
process exits at once with 0, so the boot unit can stay installed.
"""

import argparse
import json
import os
import signal
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

TAG = "bf-cec: "
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.join(ROOT, "data", "settings", "cec.json")
CEC_DEVICE = "/dev/cec0"
OSD_NAME = "BlissFlixx"
INPUT_NAME = "vc4-hdmi"
SERVER = "http://127.0.0.1:%d/api/playr"

#: Linux key codes the TV forwards, and the player action each one means.
#: Play and pause both toggle: the player's pause verb is a toggle, and the
#: interface sends it for both buttons.
ACTIONS = {
    200: "pause",    # KEY_PLAYCD, CEC play
    201: "pause",    # KEY_PAUSECD, CEC pause
    352: "pause",    # KEY_OK, CEC select
    105: "minus30",  # KEY_LEFT
    106: "plus30",   # KEY_RIGHT
    103: "plus600",  # KEY_UP
    108: "minus600", # KEY_DOWN
    174: "restart",  # KEY_EXIT, CEC back: go to the start
}

#: struct input_event as the kernel lays it out here: two longs, a timeval,
#: then type, code and value. "@" makes the sizes native, so the same code
#: reads 16-byte events on the 32-bit boxes and 24-byte events on the 64-bit.
EVENT = struct.Struct("@llHHi")
EV_KEY = 1
PRESSED = 1


def action_for(event_type, code, value):
    """The player action for a key press, or None for anything else."""
    if event_type != EV_KEY or value != PRESSED:
        return None
    return ACTIONS.get(code)


def find_input_device(text):
    """
    The event device of the CEC remote, from /proc/bus/input/devices.

    The number changes between boots and between machines, so it is looked up
    by the name the kernel gives the device, not written down.
    """
    for block in text.strip().split("\n\n"):
        if 'Name="%s"' % INPUT_NAME not in block:
            continue
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                for word in line.split("=", 1)[1].split():
                    if word.startswith("event"):
                        return "/dev/input/" + word
    return None


def physical_address(text):
    """The address the adapter reports, e.g. '1.0.0.0', or None."""
    for line in text.splitlines():
        if "Physical Address" in line and ":" in line:
            value = line.split(":", 1)[1].strip()
            if value and value != "f.f.f.f":
                return value
    return None


def enabled(path=SETTINGS):
    try:
        with open(path, "r") as f:
            return bool(json.load(f).get("enabled"))
    except (OSError, ValueError):
        return False


def cec(*args):
    try:
        return subprocess.run(
            ["cec-ctl", "-d", CEC_DEVICE] + list(args),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        print(TAG + "error cec-ctl is not available: %s" % exc, flush=True)
        return subprocess.CompletedProcess([], 1, "", "")


def claim():
    """Be a playback device, then say the Pi is the active source."""
    if cec("--playback", "-o", OSD_NAME).returncode != 0:
        print(TAG + "error could not configure " + CEC_DEVICE, flush=True)
        return False
    listing = cec("-S").stdout
    address = physical_address(listing) or "1.0.0.0"
    cec("--active-source", "phys-addr=" + address)
    print(TAG + "claimed as %s, active source %s" % (OSD_NAME, address), flush=True)
    return True


def release():
    cec("-C")


def send(port, action):
    query = urllib.parse.urlencode(
        {"fn": "control", "data": json.dumps({"action": action})}
    )
    url = SERVER % port + "?" + query
    try:
        urllib.request.urlopen(url, timeout=5).read()
    except OSError as exc:
        print(TAG + "error %s failed: %s" % (action, exc), flush=True)
        return
    print(TAG + "%s" % action, flush=True)


def read_keys(path, port):
    """Forward key presses until the device goes away. Returns on that."""
    size = EVENT.size
    with open(path, "rb", buffering=0) as f:
        while True:
            data = f.read(size)
            if len(data) < size:
                return
            _sec, _usec, event_type, code, value = EVENT.unpack(data)
            action = action_for(event_type, code, value)
            if action:
                send(port, action)


def run(port, retry):
    stopping = {"now": False}

    def stop(signum, frame):
        stopping["now"] = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    while not stopping["now"]:
        try:
            with open("/proc/bus/input/devices", "r") as f:
                path = find_input_device(f.read())
        except OSError:
            path = None
        if path is None or not claim():
            time.sleep(retry)
            continue
        try:
            read_keys(path, port)
        except OSError as exc:
            print(TAG + "error reading %s: %s" % (path, exc), flush=True)
        print(TAG + "remote went away; waiting for it to come back", flush=True)
        time.sleep(retry)
    release()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=80)
    parser.add_argument("--settings", default=SETTINGS)
    parser.add_argument("--retry", type=float, default=5.0)
    args = parser.parse_args(argv)
    if not enabled(args.settings):
        print(TAG + "off: %s does not enable it" % args.settings, flush=True)
        return 0
    return run(args.port, args.retry)


if __name__ == "__main__":
    sys.exit(main())

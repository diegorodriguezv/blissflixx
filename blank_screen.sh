#!/usr/bin/env bash
set -euo pipefail

# Screen blanking helper.
# - Lite/console installs: prefer kernel cmdline consoleblank=<seconds>, configured
#   by `configure.sh -blank` (takes effect after reboot).
# - X/desktop sessions: best-effort DPMS via xset when an X display is available.
#   (Runtime behavior on X installs is validated separately.)

# Best-effort DPMS for X sessions (no infinite retry/hang).
if command -v xset >/dev/null 2>&1; then
  displays=()
  [ -n "${DISPLAY:-}" ] && displays+=("$DISPLAY")
  displays+=(":0")
  for d in "${displays[@]}"; do
    if DISPLAY="$d" xset q >/dev/null 2>&1; then
      DISPLAY="$d" xset dpms 600 600 600 >/dev/null 2>&1 || true
      echo "DPMS enabled on DISPLAY=$d"
      exit 0
    fi
  done
fi

# Nothing to do in non-X environments (consoleblank, if configured, is kernel-managed).
exit 0

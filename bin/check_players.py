#!/usr/bin/env python3

"""
Report whether the installed players can actually run.

configure.sh installs packages; this checks that what arrived is usable. The
point is that installing a backend is not the same as being able to run it:

- omxplayer has no package on current Raspberry Pi OS, so installing it fails
  and the install must not depend on it.
- v4l2h264dec, which the GStreamer pipeline uses for hardware decode, is not in
  stock GStreamer 1.24 at all. The v4l2 decoders were dropped from the main
  gst-plugins set, and gstreamer1.0-libav provides the software avdec_h264
  instead. A pipeline naming a missing element exits immediately with
  "no element v4l2h264dec".
- The KMS and ALSA device names in the defaults are specific to one model.

So rather than assume, this asks the registry what the configured defaults need
and reports what is missing, with the setting to change.

Checks are derived from each backend's defaults, so a new backend or a new
required element is covered without editing this.
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.player.backends import backend_names, resolve_config  # noqa: E402

#: GStreamer elements a pipeline cannot do without. Checked by asking
#: gst-inspect-1.0, since the element set depends on which plugin packages are
#: installed rather than on a fixed list here.
_GST_DECODER_KEYS = ("video_decoder", "audio_decoder")


class Check:
    """One thing that has to be present for a backend to run."""

    def __init__(self, backend, label, kind, value):
        self.backend = backend
        self.label = label
        self.kind = kind
        self.value = value

    def __repr__(self):
        return "Check(%r, %r, %r, %r)" % (
            self.backend,
            self.label,
            self.kind,
            self.value,
        )

    def to_dict(self):
        return {
            "backend": self.backend,
            "label": self.label,
            "kind": self.kind,
            "value": self.value,
        }


class Result:
    def __init__(self, check, ok, detail=""):
        self.check = check
        self.ok = ok
        self.detail = detail

    def to_dict(self):
        return dict(self.check.to_dict(), ok=self.ok, detail=self.detail)


def plan_checks(name, config=None):
    """
    What has to be present for the named backend to run.

    Derived from defaults, so this stays correct as backends change.
    """
    if config is None:
        config = resolve_config(name)
    checks = [Check(name, "binary", "executable", config["binary"])]

    if "extra_args" in config:
        # Nothing to check, but noting the flags would help someone reading a
        # failure. Recorded as informational rather than verified.
        checks.append(
            Check(name, "flags", "informational", " ".join(config["extra_args"]))
        )

    for key in _GST_DECODER_KEYS:
        if key in config:
            checks.append(Check(name, key, "gst_element", config[key]))

    # These two appear inside the pipeline template rather than as their own
    # keys, but a missing one aborts the pipeline just as surely.
    if "pipeline" in config:
        for element in ("kmssink", "alsasink", "matroskademux", "queue", "h264parse"):
            if any(element in part for part in config["pipeline"]):
                checks.append(Check(name, element, "gst_element", element))

    return checks


def run_check(check, has_gst=True):
    if check.kind == "executable":
        found = (
            shutil.which(check.value)
            if os.sep not in check.value
            else (check.value if os.path.exists(check.value) else None)
        )
        return Result(check, found is not None, found or "not on PATH")

    if check.kind == "informational":
        return Result(check, True, check.value)

    if check.kind == "gst_element":
        if not has_gst:
            return Result(check, False, "gst-inspect-1.0 not available, cannot verify")
        proc = subprocess.run(
            ["gst-inspect-1.0", check.value],
            capture_output=True,
            text=True,
        )
        ok = proc.returncode == 0
        return Result(
            check,
            ok,
            "available" if ok else "no such element or plugin",
        )

    return Result(check, False, "unknown check kind " + check.kind)


def run_all(names=None, has_gst=True):
    if names is None:
        names = backend_names()
    results = []
    for name in names:
        try:
            config = resolve_config(name)
        except KeyError as exc:
            results.append(Result(Check(name, "config", "unknown", str(exc)), False))
            continue
        for check in plan_checks(name, config):
            results.append(run_check(check, has_gst=has_gst))
    return results


def render(results, active=None):
    """
    Group results under one heading per backend.

    Grouped explicitly rather than relying on the caller passing them in order,
    so a heading cannot repeat if a backend's results are ever interleaved.
    """
    grouped = {}
    for r in results:
        grouped.setdefault(r.check.backend, []).append(r)

    lines = []
    for backend, items in grouped.items():
        marker = " (active)" if backend == active else ""
        lines.append("")
        lines.append(backend + marker)
        for r in items:
            if r.check.kind == "informational":
                lines.append("    flags        " + r.detail)
                continue
            lines.append(
                "    [%s] %-14s %s%s"
                % (
                    "ok" if r.ok else "MISSING",
                    r.check.value,
                    r.check.label,
                    "" if r.ok else "  <- " + r.detail,
                )
            )
    return "\n".join(lines)


#: What to change when something is missing. Kept here so the advice and the
#: check live together.
REMEDY = {
    "omxplayer": "no package on current Raspberry Pi OS; the legacy backends "
    "only work on the old OS. Select another backend with data/settings/player",
    "omxplayer-keys": "no package on current Raspberry Pi OS; see omxplayer",
    "vlc": "apt install vlc",
    "mpv": "apt install mpv",
    "gstreamer": "apt install gstreamer1.0-tools gstreamer1.0-plugins-base "
    "gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav; "
    "if video_decoder is missing, stock GStreamer has no v4l2h264dec, so set "
    'video_decoder to "avdec_h264" in data/settings/player-gstreamer',
}


def render_remedies(results):
    """A short line per backend with something missing."""
    broken = []
    for r in results:
        if not r.ok and r.check.kind != "informational":
            broken.append(r.check.backend)
    lines = []
    for name in sorted(set(broken)):
        lines.append("  " + name + ": " + REMEDY.get(name, "see the README"))
    return "\n".join(lines)


def is_ready(results, backend):
    """Whether every real check for a backend passed."""
    relevant = [
        r
        for r in results
        if r.check.backend == backend and r.check.kind != "informational"
    ]
    return bool(relevant) and all(r.ok for r in relevant)


def missing_for(results, backend):
    return [
        r.check.value
        for r in results
        if r.check.backend == backend and not r.ok and r.check.kind != "informational"
    ]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    from lib.player.backends import active_backend_name

    active = active_backend_name()
    wanted = [a for a in argv if not a.startswith("-")] or None
    results = run_all(wanted)

    print("Player backend readiness")
    print(render(results, active=active))
    print("")

    # Only judge the active backend when it was actually among the checks.
    # Asking about one backend is a diagnostic for that backend; saying the
    # untouched active one is "not ready" would be both wrong and unexplained.
    checked = list(dict.fromkeys(r.check.backend for r in results))
    if wanted is None or active in checked:
        if not is_ready(results, active):
            print("The active backend (%s) is NOT ready:" % active)
            print(render_remedies(results))
            return 1
        print("The active backend (%s) is ready." % active)
    else:
        print(
            "Active backend is %s, which was not among the backends checked." % active
        )

    others = [n for n in checked if n != active and not is_ready(results, n)]
    if others:
        print("")
        print("Not ready, and not in use:")
        for name in others:
            print("  %s: %s" % (name, ", ".join(missing_for(results, name))))
        print("")
        print("To fix, install or reconfigure them:")
        print(render_remedies(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
What the UPDATE button does, and what it has to say afterwards.

Three separate things move when it is pressed, and they fail for different
reasons, so they are done separately and reported separately:

  * the plugins, which are their own checkouts;
  * yt-dlp, which is a package and follows the sites it extracts from;
  * BlissFlixx itself.

The one thing deliberately not moved is requirements.txt. Its contents are
pinned, and a pinned dependency is only pinned if something does not quietly
update it -- so when a pull brings a changed one, this says so and leaves it.

yt-dlp is the awkward one, because it is the component most likely to break
playback when it moves. So it is moved, then asked to prove it works, and put
back if it cannot. The alternative -- one button that updates the code and the
extractor together, with nothing checking either -- was not worth having.
"""

import json
import os
import subprocess
import sys
import time

import lib.gitutils as gitutils
import lib.locations as locations

#: Where the outcome is written, so it survives the restart the button causes.
#: The page reloads when it comes back, and without this a successful update
#: and an update that changed nothing look identical after that reload.
LAST_UPDATE_PATH = os.path.join(locations.DATA_PATH, "last-update.json")

#: Something for the smoke test to resolve.
#:
#: A long-lived public domain film on a host that has been up for decades. It
#: is deliberately a plain media URL rather than a site: a site's availability
#: is not evidence about yt-dlp, since YouTube answers "sign in to confirm you
#: are not a bot" to a Raspberry Pi on any version, and a smoke test that fails
#: for that reason is a smoke test that gets ignored.
YTDLP_SMOKE_URL = (
    "https://archive.org/download/BigBuckBunny_124/Content/"
    "big_buck_bunny_720p_surround.mp4"
)


class Part:
    """One thing that was moved, or why it was not."""

    def __init__(self, name, ok, changed, detail="", note=""):
        self.name = name
        self.ok = ok
        self.changed = changed
        self.detail = detail
        self.note = note

    def as_dict(self):
        return {
            "name": self.name,
            "ok": self.ok,
            "changed": self.changed,
            "detail": self.detail,
            "note": self.note,
        }

    def one_line(self):
        if not self.ok:
            return "%s: not updated -- %s" % (self.name, self.detail)
        if self.changed:
            return "%s: %s%s" % (
                self.name,
                self.detail,
                (" -- " + self.note) if self.note else "",
            )
        return "%s: already current" % self.name


def _run(cmd, timeout):
    """Run something, returning (ok, output). git's failures come back the same
    way; this exists so that a network problem reads as a sentence."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return False, str(exc)
    except subprocess.TimeoutExpired:
        return False, "took longer than %s seconds" % timeout
    except OSError as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def ytdlp_version():
    """What is installed, or None if there is nothing runnable."""
    ok, out = _run([sys.executable, "-m", "yt_dlp", "--version"], 60)
    return out.strip().splitlines()[0] if ok and out.strip() else None


def ytdlp_smoke_test():
    """
    Does the freshly installed yt-dlp still resolve something?

    The arguments are the ones lib/player/ytdlproc.py builds, because that is
    the contract that matters: if a version stops accepting one of them, every
    video fails and nothing else would say so until someone pressed play.
    """
    ok, out = _run(
        [
            sys.executable,
            "-m",
            "yt_dlp",
            "--js-runtimes",
            "node",
            "--simulate",
            "--no-playlist",
            "--skip-download",
            "--print",
            "%(url)#j",
            YTDLP_SMOKE_URL,
        ],
        180,
    )
    answer = out.strip().splitlines()[-1] if out.strip() else ""
    if not ok:
        return False, out.strip().splitlines()[-1] if out.strip() else "it said nothing"
    if not answer or answer in ("NA", "null", '"NA"'):
        return False, "it resolved nothing for a URL it is expected to handle"
    return True, answer[:80]


def update_ytdlp():
    """
    Move yt-dlp to the newest release, and put it back if it cannot work.

    Not pinned, deliberately: it follows the sites rather than this project, so
    a pinned one stops handling some of them. What protects playback is not the
    pin but the check afterwards.
    """
    before = ytdlp_version()
    if before is None:
        return Part(
            "yt-dlp",
            ok=False,
            changed=False,
            detail=(
                "there is no yt-dlp in this virtualenv. Run ./configure_py.sh "
                "to install one."
            ),
        )

    ok, out = _run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]"],
        900,
    )
    if not ok:
        return Part(
            "yt-dlp",
            ok=False,
            changed=False,
            detail=_last_line(out) or "pip could not reach an index",
        )

    after = ytdlp_version()
    if after == before:
        return Part("yt-dlp", ok=True, changed=False, detail=before)

    passed, why = ytdlp_smoke_test()
    if passed:
        return Part(
            "yt-dlp",
            ok=True,
            changed=True,
            detail="%s -> %s" % (before, after),
        )

    # It did not work, so it goes back. pip has the wheel it came from, so this
    # needs no network and no second attempt at finding out.
    _run([sys.executable, "-m", "pip", "install", "yt-dlp==" + before], 600)
    back = ytdlp_version()
    return Part(
        "yt-dlp",
        ok=False,
        changed=False,
        detail="the new version did not work: %s" % why,
        note="put back to %s" % (back or before),
    )


def update_blissflixx():
    """Pull this checkout, and say whether that brought a new requirements.txt."""
    before = gitutils.head_sha(locations.ROOT_PATH)
    result = gitutils.pull(locations.ROOT_PATH, name="blissflixx")
    part = Part(
        result.name,
        ok=result.ok,
        changed=result.changed,
        detail=result.detail,
    )
    if result.ok and result.changed:
        if gitutils.requirements_changed(locations.ROOT_PATH, before, result.detail):
            part.note = (
                "requirements.txt changed. Run ./configure_py.sh to install "
                "what the new version needs."
            )
    return part


def run():
    """
    Do everything the button does, and report each part.

    Returns (parts, changed). "changed" is whether anything moved at all, which
    is the difference between restarting the server and saying there was nothing
    to do -- and it is why the button is not just always the same: an update
    that changes nothing should not take the machine down to say so.
    """
    parts = []

    parts.extend(
        Part(
            r.name,
            ok=r.ok,
            changed=r.changed,
            detail=r.detail,
        )
        for r in gitutils.pull_subdirs(locations.PLUGIN_PATH)
    )

    parts.append(update_ytdlp())
    parts.append(update_blissflixx())

    changed = any(p.changed for p in parts)
    _remember(parts, changed)
    return parts, changed


def _last_line(text):
    lines = [line for line in (text or "").splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def _remember(parts, changed):
    """Write the outcome down, because the restart that follows loses the reply
    that would have carried it."""
    payload = {
        "at": int(time.time()),
        "changed": changed,
        "parts": [p.as_dict() for p in parts],
    }
    try:
        os.makedirs(os.path.dirname(LAST_UPDATE_PATH), exist_ok=True)
        with open(LAST_UPDATE_PATH, "w") as handle:
            json.dump(payload, handle, indent=2)
    except OSError:
        # Not worth failing an update over: the answer is still in the reply
        # when there is one.
        pass


def last_update():
    """The last recorded outcome, or None if there has not been one."""
    try:
        with open(LAST_UPDATE_PATH) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None

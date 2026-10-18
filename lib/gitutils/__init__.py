"""
git operations for the update button, and for installing plugins.

Every function here reports rather than raises. git failing is an everyday
event -- a machine with no network, a checkout with a local edit in it, a
directory that is not a clone -- and the person who pressed the button needs to
be told which one, in a sentence, rather than handed a traceback while the
interface goes on claiming an update is happening.
"""

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor

import cherrypy


class GitResult:
    """
    What one pull did, or why it did not.

    A named tuple would do, except that it prints as a tuple in a message and
    this gets read by people.
    """

    def __init__(self, name, ok, changed, detail=""):
        self.name = name
        self.ok = ok
        self.changed = changed
        self.detail = detail

    def __repr__(self):
        return "GitResult(%r, ok=%r, changed=%r, %r)" % (
            self.name,
            self.ok,
            self.changed,
            self.detail,
        )


def _run(cmd, cwd, timeout=120):
    """Run git, returning (ok, output). Never raises for git's own failures."""
    try:
        done = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "git is not installed on this machine"
    except subprocess.TimeoutExpired:
        return False, "git took longer than %s seconds and was given up on" % timeout
    except OSError as exc:
        return False, str(exc)
    return done.returncode == 0, (done.stdout + done.stderr).strip()


def is_clone(dirpath):
    return os.path.exists(os.path.join(dirpath, ".git"))


def current_branch(dirpath):
    ok, out = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], dirpath, timeout=30)
    return out.strip() if ok else None


def head_sha(dirpath):
    ok, out = _run(["git", "rev-parse", "--short", "HEAD"], dirpath, timeout=30)
    return out.strip() if ok else None


def clone(dirpath, repo):
    _run(["git", "clone", repo], os.path.dirname(dirpath) or ".")


#: How many pulls to run at once. Every plugin pulling at once is a burst of
#: git processes on a Pi 3, for no gain: these are small repositories over a
#: network that is the bottleneck either way.
MAX_PARALLEL_PULLS = 4


def pull(dirpath, name=None):
    """
    Fast-forward one checkout to the newest commit on the branch it is on.

    The branch is read rather than written down. It used to be "master", which
    was right for yt-dlp and wrong for everything else: BlissFlixx lives on
    bookworm, so the pull asked for a branch 138 commits behind and merged the
    two commits that were on it, reporting success and updating nothing that
    mattered.

    --ff-only is the other half and matters more. Without it git resolves any
    divergence by merging, so a checkout that has gone its own way is rewritten
    in silence. With it, divergence is a failure with an exit status, which is
    the only version of this that can be reported honestly.
    """
    name = name or os.path.basename(dirpath.rstrip("/")) or dirpath

    if not is_clone(dirpath):
        return GitResult(
            name,
            ok=False,
            changed=False,
            detail="not a git checkout, so there is nothing to pull",
        )

    branch = current_branch(dirpath)
    if not branch:
        return GitResult(
            name, ok=False, changed=False, detail="git cannot say which branch this is"
        )

    before = head_sha(dirpath)
    ok, out = _run(["git", "pull", "--ff-only", "origin", branch], dirpath)
    if not ok:
        # git's own words, trimmed: its last line is the one that says why, and
        # the rest is advice about remotes that does not apply here.
        said = [line for line in out.splitlines() if line.strip()]
        detail = said[-1] if said else "git pull failed and said nothing"
        return GitResult(name, ok=False, changed=False, detail=detail)

    after = head_sha(dirpath)
    return GitResult(name, ok=True, changed=before != after, detail=after or "")


def pull_subdirs(dirpath):
    """
    Pull every checkout under a directory, a few at a time.

    This used to chdir into each one in turn from its own thread. chdir is
    process-wide, so with several plugins in flight one thread's pull ran in
    whichever directory another thread had most recently moved to: some plugins
    never updated, some were pulled twice, and nothing said so. Passing cwd to
    the subprocess removes the shared state entirely.
    """
    if not os.path.isdir(dirpath):
        return []

    repos = [
        os.path.join(dirpath, entry)
        for entry in sorted(os.listdir(dirpath))
        if is_clone(os.path.join(dirpath, entry))
    ]
    if not repos:
        return []

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL_PULLS) as pool:
        return list(pool.map(pull, repos))


def requirements_changed(dirpath, old_sha, new_sha):
    """
    Whether a pull brought a different requirements.txt with it.

    The dependencies are pinned and the update button does not move them, so it
    cannot know whether the new code needs something installed. But it can say
    that the file changed, which is the difference between "restart and hope"
    and "restart, and then run configure_py.sh".
    """
    if not old_sha or not new_sha or old_sha == new_sha:
        return False
    ok, out = _run(
        [
            "git",
            "diff",
            "--name-only",
            "%s..%s" % (old_sha, new_sha),
            "--",
            "requirements.txt",
        ],
        dirpath,
        timeout=30,
    )
    return ok and "requirements.txt" in out

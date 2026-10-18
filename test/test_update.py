"""The UPDATE button: what it moves, what it refuses to move, and what it says.

The old restart path was not tested at all -- "Restart is destructive -- it kills
the process with SIGUSR2 -- and is not exercised here" -- and the things that
were wrong with it were the sort you only find by pressing the button: it pulled
a branch this project does not use, it announced itself before knowing whether
anything would happen, and a failure left it announcing itself for ever.

So it is tested here, by driving updater.run() with git, pip and the smoke test
stubbed. The one line left untested is the kill, which is a line.
"""

import json
import os
import subprocess
import sys

import pytest

import lib.updater as updater
from lib import gitutils


@pytest.fixture
def api():
    """The handler, with the venv check already satisfied -- the suite is run
    with the right interpreter, so require_virtualenv() has nothing to say."""
    import blissflixx

    return blissflixx.Api()


@pytest.fixture(autouse=True)
def _nothing_is_really_run(monkeypatch, tmp_path):
    """
    No test in here touches the network, a real checkout, or a real pip.

    Recorded so that a test which forgets to stub something fails as "it tried to
    run something" rather than quietly succeeding on this machine.
    """
    ran = []

    def record(cmd, timeout):
        ran.append(cmd)
        return True, ""

    # updater._run, not updater.subprocess.run. Both modules reach the same
    # stdlib subprocess object, so patching that here leaked into the gitutils
    # tests below -- which are specifically about what happens when git really
    # fails.
    monkeypatch.setattr(updater, "_run", record)
    monkeypatch.setattr(updater, "LAST_UPDATE_PATH", str(tmp_path / "last-update.json"))
    return ran


def stub_git(monkeypatch, results, requirements=False):
    """Put canned answers where the updater looks for them."""
    monkeypatch.setattr(
        updater.gitutils,
        "pull_subdirs",
        lambda path: [
            gitutils.GitResult(r.name, r.ok, r.changed, r.detail) for r in results
        ],
    )
    monkeypatch.setattr(updater.gitutils, "head_sha", lambda path: "aaaaaaa")
    monkeypatch.setattr(
        updater.gitutils,
        "pull",
        lambda path, name=None: gitutils.GitResult(
            name or "blissflixx", True, True, "bbbbbbb"
        ),
    )
    monkeypatch.setattr(
        updater.gitutils, "requirements_changed", lambda *a, **k: requirements
    )


class TestNothingIsAnnouncedUntilItIsTrue:
    """
    RESTARTING used to be set before any work happened.

    So the bar said "Server Restarting & Updating..." from the moment the button
    was pressed whether or not anything was updated -- and if a pull then failed,
    nothing took it down again. Every tab on the machine sat on that message for
    as long as it was open, about an update that was not happening.
    """

    def test_nothing_changed_means_nothing_restarts(self, api, monkeypatch):
        import blissflixx

        stub_git(monkeypatch, [gitutils.GitResult("bfch_xnxx", True, False)])
        monkeypatch.setattr(
            updater.gitutils,
            "pull",
            lambda path, name=None: gitutils.GitResult(name, True, False, "aaaaaaa"),
        )
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        killed = []
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: killed.append(sig))

        answer = api.default("server", fn="restart")

        assert killed == [], "the process was killed for an update that changed nothing"
        assert answer["updated"] is False
        assert "already current" in answer["message"]

    def test_and_the_server_is_not_left_saying_it_is_updating(self, api, monkeypatch):
        import blissflixx

        stub_git(monkeypatch, [gitutils.GitResult("bfch_xnxx", True, False)])
        monkeypatch.setattr(
            updater.gitutils,
            "pull",
            lambda path, name=None: gitutils.GitResult(name, True, False, "aaaaaaa"),
        )
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: None)

        try:
            api.default("server", fn="restart")
            assert (
                blissflixx.RESTARTING is False
            ), "the machine is now claiming an update is in progress that is not"
        finally:
            blissflixx.RESTARTING = False

    def test_something_changed_means_it_restarts(self, api, monkeypatch):
        import blissflixx

        stub_git(monkeypatch, [gitutils.GitResult("bfch_xnxx", True, True, "c1c1c1c")])
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        killed = []
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: killed.append(sig))

        try:
            api.default("server", fn="restart")
            assert killed, "something moved and the process was not restarted"
            assert blissflixx.RESTARTING is True
        finally:
            blissflixx.RESTARTING = False

    def test_a_failure_does_not_restart_and_does_not_leave_the_flag_set(
        self, api, monkeypatch
    ):
        """
        The state that was impossible to get out of. A pull that fails used to
        leave RESTARTING true, because nothing ever cleared it.
        """
        import blissflixx

        monkeypatch.setattr(
            updater.gitutils,
            "pull_subdirs",
            lambda path: [gitutils.GitResult("bfch_xnxx", False, False, "no network")],
        )
        monkeypatch.setattr(
            updater.gitutils,
            "pull",
            lambda path, name=None: gitutils.GitResult(
                name, False, False, "local changes"
            ),
        )
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        killed = []
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: killed.append(sig))

        try:
            answer = api.default("server", fn="restart")
            assert killed == [], "it restarted despite every part failing"
            assert blissflixx.RESTARTING is False
        finally:
            blissflixx.RESTARTING = False

        assert answer["updated"] is False
        # And it says what to do about it, which is the other half: the old path
        # handed back a Python traceback.
        assert "no network" in answer["message"]
        assert "local changes" in answer["message"]
        assert "configure_py.sh" in answer["message"]
        assert "Traceback" not in json.dumps(answer)


def fake_git(
    monkeypatch, repo, pull_ok=True, before="aaaaaaa", after="aaaaaaa", said=""
):
    """
    Answer git's questions from a script, and record what it was asked.

    Every call is recorded rather than matched, so a test can assert on the whole
    command line instead of on a branch of it.
    """
    asked = []
    heads = [before, after]

    def fake_run(cmd, cwd, timeout=120):
        asked.append(list(cmd))
        if cmd[1] == "pull":
            return pull_ok, said
        if cmd[1] == "rev-parse" and "--abbrev-ref" in cmd:
            return True, "bookworm\n"
        if cmd[1] == "rev-parse":
            # Asked once before the pull and once after. Answering "before"
            # both times is how a pull that moved things looks like one that did
            # not.
            return True, (heads.pop(0) if len(heads) > 1 else after) + "\n"
        return True, ""

    monkeypatch.setattr(gitutils, "_run", fake_run)
    return asked


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


class TestTheBranchIsReadNotWrittenDown:
    """
    It pulled "master", which is right for yt-dlp and wrong for everything else.

    BlissFlixx lives on bookworm, so the pull asked for a branch 138 commits
    behind and merged the two commits that were on it: reported success, updated
    nothing that mattered, and left the branch with a merge in it.
    """

    def test_the_branch_asked_for_is_the_one_checked_out(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        asked = fake_git(monkeypatch, repo, after="abc1234")

        gitutils.pull(str(repo), name="blissflixx")

        pulls = [cmd for cmd in asked if cmd[1] == "pull"]
        assert pulls, "no pull was run"
        assert "bookworm" in pulls[0], pulls[0]
        assert "master" not in pulls[0], pulls[0]

    def test_and_it_will_not_merge_itself_into_a_diverged_branch(
        self, tmp_path, monkeypatch
    ):
        """
        --ff-only turns divergence into a failure with an exit status, which is
        the only version of this that can be reported honestly. Without it git
        resolves the difference in silence and rewrites the branch.
        """
        repo = make_repo(tmp_path)
        asked = fake_git(monkeypatch, repo, after="abc1234")

        gitutils.pull(str(repo), name="blissflixx")

        pulls = [cmd for cmd in asked if cmd[1] == "pull"]
        assert "--ff-only" in pulls[0], pulls[0]

    def test_and_a_moved_checkout_says_so(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        fake_git(monkeypatch, repo, before="aaaaaaa", after="abc1234")

        result = gitutils.pull(str(repo), name="blissflixx")

        assert result.ok is True
        assert result.changed is True
        assert result.detail == "abc1234", result.detail

    def test_and_one_that_did_not_move_does_not(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        fake_git(monkeypatch, repo, before="aaaaaaa", after="aaaaaaa")

        result = gitutils.pull(str(repo), name="blissflixx")

        assert result.ok is True
        assert result.changed is False


class TestWhatAFailureSays:
    def test_gits_own_words_are_used(self, tmp_path, monkeypatch):
        repo = make_repo(tmp_path)
        fake_git(
            monkeypatch,
            repo,
            pull_ok=False,
            said="hint: Tracking is not set up\nfatal: refusing to merge unrelated",
        )

        result = gitutils.pull(str(repo), name="blissflixx")

        assert result.ok is False
        # The last line, which is the one that says why. The rest is advice about
        # remotes that does not apply to a checkout that already has an origin.
        assert result.detail == "fatal: refusing to merge unrelated", result.detail

    def test_a_directory_that_is_not_a_checkout_is_reported_not_ignored(self, tmp_path):
        """
        The guard that returned silently is why one machine's yt-dlp could not
        be updated at all and the button reported success.
        """
        plain = tmp_path / "not-a-clone"
        plain.mkdir()

        result = gitutils.pull(str(plain), name="bfch_thing")

        assert result.ok is False
        assert "not a git checkout" in result.detail

    def test_and_nothing_runs_git_at_all_for_one(self, tmp_path, monkeypatch):
        asked = []
        monkeypatch.setattr(
            gitutils,
            "_run",
            lambda cmd, cwd, timeout=120: asked.append(cmd) or (True, ""),
        )
        plain = tmp_path / "not-a-clone"
        plain.mkdir()

        gitutils.pull(str(plain), name="bfch_thing")

        assert asked == [], "git was run on something that is not a checkout"

    def test_and_git_being_absent_is_a_sentence_not_a_traceback(self, tmp_path):
        ok, said = gitutils._run(["/nonexistent/git", "pull"], str(tmp_path))
        assert ok is False
        assert said, "a missing git said nothing at all"

    def test_and_a_timeout_is_a_sentence(self, tmp_path, monkeypatch):
        def explode(cmd, cwd, timeout=120, **kwargs):
            raise subprocess.TimeoutExpired(cmd, timeout)

        monkeypatch.setattr(gitutils.subprocess, "run", explode)
        ok, said = gitutils._run(["git", "pull"], str(tmp_path), timeout=7)
        assert ok is False
        assert "7 seconds" in said, said


class TestYtdlpIsCheckedAfterItMoves:
    """
    It is the component most likely to break playback when it moves, so it is
    moved and then asked to prove it works.
    """

    def _pip(self, monkeypatch, versions, smoke):
        """Answer pip, the version command and the smoke test from a script."""
        # Starts at whatever is installed now, so the first --version answer is
        # that one and the upgrade has something to move away from.
        state = {"version": versions[0]}

        def fake_run(cmd, timeout):
            if "pip" in cmd:
                if cmd[cmd.index("install") + 1].startswith("--upgrade"):
                    state["version"] = versions[1]
                else:
                    state["version"] = cmd[-1].split("==")[-1]
                return True, "Successfully installed"
            if "--version" in cmd:
                return True, state["version"] if state["version"] else ""
            return smoke

        monkeypatch.setattr(updater, "_run", fake_run)

    def test_a_working_version_is_kept_and_reported(self, monkeypatch):
        self._pip(
            monkeypatch, ("2025.12.08", "2026.08.19"), (True, "https://example/x.mp4")
        )

        part = updater.update_ytdlp()

        assert part.ok is True
        assert part.changed is True
        assert part.detail == "2025.12.08 -> 2026.08.19", part.detail

    def test_one_that_does_not_work_is_put_back(self, monkeypatch):
        self._pip(
            monkeypatch, ("2025.12.08", "2026.08.19"), (False, "it resolved nothing")
        )

        part = updater.update_ytdlp()

        assert part.ok is False
        assert part.changed is False, "a version that failed was reported as an update"
        assert "2025.12.08" in part.note, part.note

    def test_and_the_smoke_test_uses_the_arguments_the_player_uses(self, monkeypatch):
        """
        If a version stops accepting one of them, every video fails and nothing
        says so until somebody presses play.
        """
        seen = {}

        def fake_run(cmd, timeout):
            seen["cmd"] = cmd
            return True, '["https://example/x.mp4"]'

        monkeypatch.setattr(updater, "_run", fake_run)

        ok, _ = updater.ytdlp_smoke_test()

        assert ok is True
        assert seen["cmd"][:3] == [sys.executable, "-m", "yt_dlp"], seen["cmd"][:3]
        for flag in (
            "--simulate",
            "--no-playlist",
            "--skip-download",
            "--print",
            "--js-runtimes",
        ):
            assert flag in seen["cmd"], flag

    def test_and_a_resolve_that_returns_nothing_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(updater, "_run", lambda cmd, timeout: (True, "NA"))
        ok, why = updater.ytdlp_smoke_test()
        assert ok is False
        assert "nothing" in why

    def test_no_yt_dlp_at_all_says_so_rather_than_crashing(self, monkeypatch):
        monkeypatch.setattr(updater, "ytdlp_version", lambda: None)
        part = updater.update_ytdlp()
        assert part.ok is False
        assert "configure_py.sh" in part.detail


class TestTheOtherDependenciesDoNotMove:
    """
    requirements.txt is pinned, and a pinned dependency is only pinned if nothing
    quietly updates it. So the button does not install it -- it says the file
    changed and leaves it.
    """

    def test_a_changed_requirements_is_reported_not_installed(self, api, monkeypatch):
        import blissflixx

        stub_git(
            monkeypatch,
            [gitutils.GitResult("bfch_xnxx", True, False)],
            requirements=True,
        )
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: None)

        try:
            api.default("server", fn="restart")
            part = updater.last_update()
            notes = [p.get("note", "") for p in part["parts"]]
            assert any("requirements.txt changed" in n for n in notes), notes
        finally:
            blissflixx.RESTARTING = False

    def test_and_an_unchanged_one_is_silent_about_it(self, api, monkeypatch):
        import blissflixx

        stub_git(
            monkeypatch,
            [gitutils.GitResult("bfch_xnxx", True, False)],
            requirements=False,
        )
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: None)

        try:
            api.default("server", fn="restart")
            notes = [p.get("note", "") for p in updater.last_update()["parts"]]
            assert not any("requirements.txt" in n for n in notes), notes
        finally:
            blissflixx.RESTARTING = False


class TestTheOutcomeOutlivesTheRestart:
    def test_it_is_written_down(self, api, monkeypatch):
        import blissflixx

        stub_git(monkeypatch, [gitutils.GitResult("bfch_xnxx", True, False)])
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: None)

        try:
            api.default("server", fn="restart")
        finally:
            blissflixx.RESTARTING = False

        recorded = updater.last_update()
        assert recorded is not None
        assert [p["name"] for p in recorded["parts"]] == [
            "bfch_xnxx",
            "yt-dlp",
            "blissflixx",
        ]

    def test_and_the_server_can_be_asked_for_it_afterwards(self, api, monkeypatch):
        import blissflixx

        stub_git(
            monkeypatch,
            [gitutils.GitResult("bfch_thing", False, False, "not a git checkout")],
        )
        monkeypatch.setattr(
            updater,
            "update_ytdlp",
            lambda: updater.Part("yt-dlp", True, False, "2026.08.19"),
        )
        monkeypatch.setattr(blissflixx.os, "kill", lambda pid, sig: None)

        try:
            api.default("server", fn="restart")
        finally:
            blissflixx.RESTARTING = False

        answer = api.default("server", fn="last_update")
        assert any("not a git checkout" in p["detail"] for p in answer["parts"])

    def test_and_reading_it_before_any_update_is_not_an_error(self, api):
        answer = api.default("server", fn="last_update")
        assert answer == {"parts": [], "changed": False}

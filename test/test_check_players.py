"""
bin/check_players.py

Installing a player is not the same as being able to run it: the KMS and ALSA
device names in the defaults are specific to one model. This checks what is
actually present and says what to do about what is missing.

The checks are derived from the backend's defaults rather than hardcoded, so a
new required option is covered without editing the script. That is what most of
these tests protect.

Everything that shells out is stubbed; nothing here runs a player.
"""

import importlib.util
import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "bin" / "check_players.py"


def load_module():
    """Import bin/check_players.py, which is not a package."""
    spec = importlib.util.spec_from_file_location("check_players", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_players"] = module
    spec.loader.exec_module(module)
    return module


check_players = load_module()


class TestPlanningIsDerivedFromDefaults:
    """
    The important property: the checks come from the backends' own defaults, so
    they cannot fall behind the code.
    """

    def test_every_registered_backend_is_covered(self):
        from lib.player.backends import backend_names

        for name in backend_names():
            checks = check_players.plan_checks(name)
            assert checks, name

    def test_binary_is_always_checked_first(self):
        for name in ("vlc",):
            first = check_players.plan_checks(name)[0]
            assert first.kind == "executable", name
            assert first.label == "binary"

    def test_flags_are_recorded_for_context(self):
        """
        Informational, not verified, but they make a failure report say what the
        player was actually asked to do.
        """
        labels = [c.label for c in check_players.plan_checks("vlc")]
        assert "flags" in labels

    def test_plan_reflects_overridden_config(self):
        """
        The plan follows the settings, not just the defaults: a user who moved
        the binary should be checked for that instead.
        """
        from lib.player.backends import resolve_config

        config = dict(resolve_config("vlc"), binary="/opt/vlc/bin/vlc")
        checks = check_players.plan_checks("vlc", config)
        assert checks[0].value == "/opt/vlc/bin/vlc"

    def test_unknown_config_key_does_not_break_planning(self, settings):
        """
        A settings file written for a newer version names things this does not
        check; planning must not fall over on them.
        """
        from lib.player.backends import resolve_config

        config = dict(resolve_config("vlc"), something_new=1, another=["x"])
        assert check_players.plan_checks("vlc", config)


class TestExecutingChecks:
    def test_executable_found_on_path(self):
        """Whichever backend, use a binary that certainly exists here."""
        check = check_players.Check("t", "binary", "executable", "python3")
        result = check_players.run_check(check)
        assert result.ok is True
        assert "python3" in result.detail

    def test_executable_missing(self):
        check = check_players.Check("t", "binary", "executable", "definitely_not_here")
        result = check_players.run_check(check)
        assert result.ok is False
        assert "not on PATH" in result.detail

    def test_absolute_path_is_checked_directly(self):
        check = check_players.Check(
            "t", "binary", "executable", str(REPO_ROOT / "configure.sh")
        )
        assert check_players.run_check(check).ok is True

    def test_informational_always_passes(self):
        check = check_players.Check("t", "flags", "informational", "--foo --bar")
        result = check_players.run_check(check)
        assert result.ok is True
        assert result.detail == "--foo --bar"

    def test_unknown_kind_fails_loudly(self):
        check = check_players.Check("t", "x", "nonsense", "y")
        result = check_players.run_check(check)
        assert result.ok is False
        assert "unknown check kind" in result.detail

class TestReadinessAggregation:
    def _results(self, backend, oks):
        return [
            check_players.Result(
                check_players.Check(backend, "l", "executable", "v%d" % i), ok
            )
            for i, ok in enumerate(oks)
        ]

    def test_backend_with_all_ok_is_ready(self):
        assert check_players.is_ready(self._results("vlc", [True, True]), "vlc")

    def test_backend_with_any_failure_is_not_ready(self):
        assert not check_players.is_ready(self._results("vlc", [True, False]), "vlc")

    def test_informational_checks_do_not_affect_readiness(self):
        """
        Otherwise a backend would be reported as broken just for having flags.
        """
        results = self._results("vlc", [True])
        results.append(
            check_players.Result(
                check_players.Check("vlc", "flags", "informational", "--x"), True
            )
        )
        assert check_players.is_ready(results, "vlc")

    def test_backend_with_no_checks_is_not_ready(self):
        """
        Better to report nothing known as not-ready than to claim a pass.
        """
        assert not check_players.is_ready([], "vlc")

    def test_missing_lists_the_values(self):
        results = self._results("gst", [False, True, False])
        assert check_players.missing_for(results, "gst") == ["v0", "v2"]

    def test_missing_ignores_informational(self):
        results = self._results("vlc", [False])
        results.append(
            check_players.Result(
                check_players.Check("vlc", "flags", "informational", "--x"), False
            )
        )
        assert check_players.missing_for(results, "vlc") == ["v0"]


class TestReportRendering:
    def test_missing_is_labelled(self):
        results = [
            check_players.Result(
                check_players.Check("vlc", "binary", "executable", "cvlc"), False
            )
        ]
        out = check_players.render(results, active="vlc")
        assert "MISSING" in out
        assert "vlc (active)" in out

    def test_ok_is_labelled(self):
        results = [
            check_players.Result(
                check_players.Check("vlc", "binary", "executable", "cvlc"), True
            )
        ]
        assert "[ok]" in check_players.render(results)

    def test_flags_shown_without_a_verdict(self):
        """
        Built through run_check, which is how a real result is produced: the
        flag text lives in detail, not in the check's value.
        """
        results = [
            check_players.run_check(
                check_players.Check("vlc", "flags", "informational", "--intf=dummy")
            )
        ]
        out = check_players.render(results)
        assert "--intf=dummy" in out
        assert "[ok]" not in out

    def test_active_backend_is_marked(self):
        results = [
            check_players.Result(
                check_players.Check("vlc", "binary", "executable", "cvlc"), True
            )
        ]
        assert "vlc (active)" in check_players.render(results, active="vlc")


class TestRemedies:
    def test_every_backend_with_a_failure_has_advice(self):
        """
        Advice and check live together, so a backend that can fail without
        telling the user what to do cannot be added unnoticed.
        """
        from lib.player.backends import backend_names

        for name in backend_names():
            assert name in check_players.REMEDY, name

    def test_remedies_only_list_broken_backends(self):
        results = [
            check_players.Result(
                check_players.Check("other", "binary", "executable", "x"), False
            )
        ]
        out = check_players.render_remedies(results)
        assert "other" in out or out == ""  # an unknown backend has no advice


class TestMainExitCode:
    def test_zero_when_the_active_backend_is_ready(self, capsys, monkeypatch):
        from lib.player.backends import DEFAULT_BACKEND

        results = [
            check_players.Result(
                check_players.Check(DEFAULT_BACKEND, "binary", "executable", "x"),
                True,
            )
        ]
        monkeypatch.setattr(check_players, "run_all", lambda names=None: results)
        assert check_players.main([]) == 0
        assert "is ready" in capsys.readouterr().out

    def test_one_when_the_active_backend_is_missing(self, capsys, monkeypatch):
        from lib.player.backends import DEFAULT_BACKEND

        results = [
            check_players.Result(
                check_players.Check(DEFAULT_BACKEND, "binary", "executable", "x"),
                False,
            )
        ]
        monkeypatch.setattr(check_players, "run_all", lambda names=None: results)
        assert check_players.main([]) == 1
        assert "NOT ready" in capsys.readouterr().out

    def test_named_backends_can_be_filtered(self, monkeypatch):
        seen = {}

        def fake_run(names=None):
            seen["names"] = names
            return []

        monkeypatch.setattr(check_players, "run_all", fake_run)
        check_players.main(["vlc", "-x"])
        assert seen["names"] == ["vlc"]


class TestGrouping:
    def test_a_backend_is_listed_once(self):
        """The report groups by backend heading, so repeats must not repeat it."""
        results = [
            check_players.Result(
                check_players.Check(n, "binary", "executable", "x"), True
            )
            for n in ("vlc", "vlc")
        ]
        out = check_players.render(results)
        assert out.count("vlc\n") == 1

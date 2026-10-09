"""
bin/check_players.py

Installing a player is not the same as being able to run it, and two of the
backends need attention on a current Raspberry Pi OS: omxplayer has no package,
and stock GStreamer has no v4l2h264dec. This checks what is actually present and
says what to do about what is missing.

The checks are derived from each backend's defaults rather than hardcoded, so a
new backend or a new required element is covered without editing the script.
That is what most of these tests protect.

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
        for name in ("vlc", "mpv", "gstreamer", "omxplayer"):
            first = check_players.plan_checks(name)[0]
            assert first.kind == "executable", name
            assert first.label == "binary"

    def test_gstreamer_decoders_are_checked(self):
        """
        v4l2h264dec is absent from stock GStreamer, so the pipeline naming it
        exits immediately. This check is what turns that into a message.
        """
        checks = check_players.plan_checks("gstreamer")
        values = {c.value for c in checks if c.kind == "gst_element"}
        assert "v4l2h264dec" in values
        assert "avdec_eac3" in values

    def test_pipeline_elements_are_checked(self):
        """
        kmssink and alsasink appear inside the template rather than as their own
        settings keys, but a missing one aborts the pipeline just as surely.
        """
        values = {c.value for c in check_players.plan_checks("gstreamer")}
        assert "kmssink" in values
        assert "alsasink" in values

    def test_non_gstreamer_backends_check_no_elements(self):
        for name in ("vlc", "mpv", "omxplayer"):
            kinds = {c.kind for c in check_players.plan_checks(name)}
            assert "gst_element" not in kinds, name

    def test_flags_are_recorded_for_context(self):
        """
        Informational, not verified, but they make a failure report say what the
        player was actually asked to do.
        """
        labels = [c.label for c in check_players.plan_checks("vlc")]
        assert "flags" in labels

    def test_plan_reflects_overridden_config(self):
        """
        The plan follows the settings, not just the defaults: a user who set
        video_decoder to avdec_h264 should be checked for that instead.
        """
        from lib.player.backends import resolve_config

        config = dict(resolve_config("gstreamer"), video_decoder="avdec_h264")
        checks = check_players.plan_checks("gstreamer", config)
        values = {c.value for c in checks if c.kind == "gst_element"}
        assert "avdec_h264" in values
        assert "v4l2h264dec" not in values

    def test_unknown_config_key_does_not_break_planning(self, settings):
        """
        A settings file written for a newer version names things this does not
        check; planning must not fall over on them.
        """
        from lib.player.backends import resolve_config

        config = dict(resolve_config("mpv"), something_new=1, another=["x"])
        assert check_players.plan_checks("mpv", config)


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

    def test_gst_element_check_when_gst_missing(self):
        """
        Without gst-inspect-1.0 the answer is unknown, not a pass. Reporting it
        as missing keeps it visible rather than silently skipped.
        """
        check = check_players.Check("t", "video_decoder", "gst_element", "x")
        result = check_players.run_check(check, has_gst=False)
        assert result.ok is False
        assert "gst-inspect-1.0 not available" in result.detail


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
                check_players.Check("mpv", "binary", "executable", "mpv"), True
            )
        ]
        assert "mpv (active)" in check_players.render(results, active="mpv")

    def test_every_backend_gets_a_heading(self):
        results = [
            check_players.Result(
                check_players.Check(n, "binary", "executable", "x"), True
            )
            for n in ("vlc", "mpv")
        ]
        out = check_players.render(results)
        assert "vlc" in out and "mpv" in out


class TestRemedies:
    def test_every_backend_with_a_failure_has_advice(self):
        """
        Advice and check live together, so a backend that can fail without
        telling the user what to do cannot be added unnoticed.
        """
        from lib.player.backends import backend_names

        for name in backend_names():
            assert name in check_players.REMEDY, name

    def test_omxplayer_says_it_cannot_be_installed(self):
        assert "no package" in check_players.REMEDY["omxplayer"]

    def test_gstreamer_names_the_substitute_decoder(self):
        """
        The actual fix on a current OS: stock GStreamer has no v4l2h264dec.
        """
        advice = check_players.REMEDY["gstreamer"]
        assert "avdec_h264" in advice
        assert "data/settings/player-gstreamer" in advice

    def test_remedies_only_list_broken_backends(self):
        results = [
            check_players.Result(
                check_players.Check("vlc", "binary", "executable", "cvlc"), True
            ),
            check_players.Result(
                check_players.Check("mpv", "binary", "executable", "mpv"), False
            ),
        ]
        out = check_players.render_remedies(results)
        assert "mpv" in out
        assert "\n  vlc:" not in out


class TestMainExitCode:
    def test_zero_when_the_active_backend_is_ready(self, capsys, monkeypatch):
        from lib.player.backends import active_backend_name

        results = [
            check_players.Result(
                check_players.Check(active_backend_name(), "binary", "executable", "x"),
                True,
            )
        ]
        monkeypatch.setattr(check_players, "run_all", lambda names=None: results)
        assert check_players.main([]) == 0
        assert "is ready" in capsys.readouterr().out

    def test_one_when_the_active_backend_is_missing(self, capsys, monkeypatch):
        from lib.player.backends import active_backend_name

        results = [
            check_players.Result(
                check_players.Check(active_backend_name(), "binary", "executable", "x"),
                False,
            )
        ]
        monkeypatch.setattr(check_players, "run_all", lambda names=None: results)
        assert check_players.main([]) == 1
        assert "NOT ready" in capsys.readouterr().out

    def test_active_failing_ignores_healthy_others(self, capsys, monkeypatch):
        """
        A broken backend that is not in use must not fail the check.
        """
        from lib.player.backends import active_backend_name

        active = active_backend_name()
        results = [
            check_players.Result(
                check_players.Check(active, "binary", "executable", "x"), True
            ),
            check_players.Result(
                check_players.Check("gstreamer", "video_decoder", "gst_element", "x"),
                False,
            ),
        ]
        monkeypatch.setattr(check_players, "run_all", lambda names=None: results)
        assert check_players.main([]) == 0
        out = capsys.readouterr().out
        assert "gstreamer" in out
        assert "Not ready, and not in use" in out

    def test_named_backends_can_be_filtered(self, monkeypatch):
        seen = {}

        def fake_run(names=None):
            seen["names"] = names
            return []

        monkeypatch.setattr(check_players, "run_all", fake_run)
        check_players.main(["vlc", "-x"])
        assert seen["names"] == ["vlc"]


class TestFilteringDoesNotMisjudgeTheActiveBackend:
    """
    Asking about one backend is a diagnostic for that backend. Reporting the
    untouched active one as "not ready" would be wrong and unexplained.
    """

    def _results(self):
        from lib.player.backends import active_backend_name

        active = active_backend_name()
        return [
            check_players.Result(
                check_players.Check(active, "binary", "executable", "cvlc"), True
            ),
            check_players.Result(
                check_players.Check("gstreamer", "video_decoder", "gst_element", "x"),
                False,
            ),
        ]

    def _stub_run_all(self, monkeypatch):
        """
        Honour the filter, the way run_all does. A stub that ignored it would
        mean the subset path is never actually exercised.
        """

        def fake_run(names=None):
            return [
                r for r in self._results() if names is None or r.check.backend in names
            ]

        monkeypatch.setattr(check_players, "run_all", fake_run)

    def test_filtered_run_explains_it_did_not_check_the_active_one(
        self, capsys, monkeypatch
    ):
        from lib.player.backends import active_backend_name

        self._stub_run_all(monkeypatch)
        assert check_players.main(["gstreamer"]) == 0
        out = capsys.readouterr().out
        assert "was not among the backends checked" in out
        assert active_backend_name() in out

    def test_filtered_run_does_not_claim_not_ready(self, capsys, monkeypatch):
        self._stub_run_all(monkeypatch)
        check_players.main(["gstreamer"])
        assert "NOT ready" not in capsys.readouterr().out

    def test_unfiltered_run_still_judges_the_active_one(self, capsys, monkeypatch):
        self._stub_run_all(monkeypatch)
        assert check_players.main([]) == 0
        out = capsys.readouterr().out
        assert "is ready" in out
        assert "was not among" not in out

    def test_filtered_run_still_reports_other_broken_backends(
        self, capsys, monkeypatch
    ):
        self._stub_run_all(monkeypatch)
        check_players.main(["gstreamer"])
        out = capsys.readouterr().out
        assert "Not ready, and not in use" in out
        assert "gstreamer: x" in out

    def test_backends_listed_once_each_in_first_seen_order(self):
        """
        The report groups by backend heading, so a repeated backend must not
        produce a second heading.
        """
        results = [
            check_players.Result(
                check_players.Check(n, "binary", "executable", "x"), True
            )
            for n in ("vlc", "mpv", "vlc", "gstreamer", "mpv")
        ]
        out = check_players.render(results)
        assert out.count("vlc\n") == 1
        assert out.count("mpv\n") == 1

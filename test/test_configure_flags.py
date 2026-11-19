"""
The installer's options.

They used to be matched as substrings of the whole argument list, so -boot was
found inside --no-boot and the install that asked to skip the boot service
installed it. A typo did nothing at all and looked like success.
"""

import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "configure.sh"


def _run(*args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )


class TestHelp:
    def test_help_prints_the_options_and_exits_zero(self):
        answer = _run("--help")
        assert answer.returncode == 0
        assert "--no-boot" in answer.stdout
        assert "-only" in answer.stdout
        assert "-uninstall" in answer.stdout

    def test_help_works_without_root(self):
        # The usage is worth reading before deciding to run the installer.
        answer = _run("-h")
        assert answer.returncode == 0
        assert "Usage:" in answer.stdout


class TestUnknownOptions:
    def test_a_typo_is_refused_rather_than_ignored(self):
        answer = _run("--no-boott")
        assert answer.returncode == 2
        assert "Unknown option: --no-boott" in answer.stderr
        assert "Usage:" in answer.stderr

    def test_boot_and_no_boot_together_are_refused(self):
        answer = _run("-boot", "--no-boot")
        assert answer.returncode == 2
        assert "either -boot or --no-boot" in answer.stderr


class TestExactMatching:
    def test_the_script_has_no_substring_flag_tests_left(self):
        source = SCRIPT.read_text()
        assert "[[ $@ == *" not in source
        assert "[[ $@ != *" not in source

    def test_each_option_has_its_own_switch(self):
        source = SCRIPT.read_text()
        for switch in (
            'NO_BOOT=0',
            'BOOT=0',
            'ONLY=0',
            'BLANK=0',
            'REJOIN=0',
            'UNINSTALL=0',
            'NO_PLAYERS=0',
        ):
            assert switch in source

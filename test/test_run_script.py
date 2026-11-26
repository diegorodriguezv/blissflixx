"""
The start script, and the one transition it has to survive.

`configure_py.sh` installs the package and generates `virtualenv/bin/blissflixx`.
An installation that pulls the change from the web updater restarts before it
has run the installer again, so `run.sh` finds the tree without the command.
The script must make the install once, then start, instead of failing to start
and leaving the box in a restart loop.
"""

import pathlib
import stat
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
RUN = ROOT / "run.sh"


def _executable(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _checkout(tmp_path, entry_point):
    """A checkout with just enough of a virtualenv for run.sh to reach it."""
    (tmp_path / "run.sh").write_bytes(RUN.read_bytes())
    (tmp_path / "virtualenv" / "bin").mkdir(parents=True)
    (tmp_path / "virtualenv" / "bin" / "activate").write_text("")
    if entry_point is not None:
        _executable(tmp_path / "virtualenv" / "bin" / "blissflixx", entry_point)
    _executable(
        tmp_path / "virtualenv" / "bin" / "pip",
        "#!/bin/sh\n"
        'echo "PIP $@"\n'
        'cat > "$(dirname "$0")/blissflixx" <<\'EOF\'\n'
        "#!/bin/sh\n"
        'echo "ENTRY $@"\n'
        "EOF\n"
        'chmod +x "$(dirname "$0")/blissflixx"\n',
    )
    return tmp_path


def _run(tmp_path):
    return subprocess.run(
        ["bash", "run.sh"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=60,
    )


class TestAnInstalledBox:
    def test_runs_the_entry_point_with_the_port(self, tmp_path):
        checkout = _checkout(tmp_path, '#!/bin/sh\necho "ENTRY $@"\n')
        proc = _run(checkout)
        assert proc.returncode == 0, proc.stderr
        assert "ENTRY --port 80" in proc.stdout
        assert "PIP " not in proc.stdout


class TestABoxThatPulledBeforeInstalling:
    def test_installs_once_then_runs_the_entry_point(self, tmp_path):
        checkout = _checkout(tmp_path, entry_point=None)
        proc = _run(checkout)
        assert proc.returncode == 0, proc.stderr
        assert "PIP install --no-deps -e ." in proc.stdout
        assert "ENTRY --port 80" in proc.stdout
        assert proc.stdout.index("PIP ") < proc.stdout.index("ENTRY ")

    def test_a_failed_install_stops_instead_of_restarting_forever(self, tmp_path):
        checkout = _checkout(tmp_path, entry_point=None)
        _executable(
            checkout / "virtualenv" / "bin" / "pip",
            "#!/bin/sh\nexit 1\n",
        )
        proc = _run(checkout)
        assert proc.returncode != 0
        assert "ENTRY " not in proc.stdout

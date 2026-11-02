"""
bin/bt_serve_retry.sh

One retry for a native crash, and only for that.

libtorrent has killed the torrent process twice on the 32-bit boxes, both times
before the port opened. A signal is retried once; the process's own failure
codes are not, because repeating those repeats a known failure. The script runs
a fake "serve" here, so nothing needs a swarm or a player.
"""

import os
import pathlib
import subprocess

WRAPPER = pathlib.Path(__file__).resolve().parents[1] / "bin" / "bt_serve_retry.sh"


def fake_serve(tmp_path, body):
    script = tmp_path / "fake_serve.sh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


def run_wrapper(tmp_path, script, args=()):
    env = dict(
        os.environ,
        BT_PYTHON="/bin/sh",
        BT_SERVE=str(script),
    )
    return subprocess.run(
        [str(WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),          # a crashing fake must not drop a core here
        timeout=30,
    )


class TestRetry:
    def test_a_signal_is_retried_once(self, tmp_path):
        counter = tmp_path / "runs"
        script = fake_serve(
            tmp_path,
            """
echo run >> %s
if [ "$(wc -l < %s)" -eq 1 ]; then
    kill -SEGV $$
fi
echo served
"""
            % (counter, counter),
        )
        done = run_wrapper(tmp_path, script)
        assert done.returncode == 0
        assert "served" in done.stdout
        assert counter.read_text().count("run") == 2

    def test_it_says_it_is_starting_again(self, tmp_path):
        counter = tmp_path / "runs"
        script = fake_serve(
            tmp_path,
            'echo run >> %s\nif [ "$(wc -l < %s)" -eq 1 ]; then kill -SEGV $$; fi\n'
            % (counter, counter),
        )
        done = run_wrapper(tmp_path, script)
        assert "killed by signal 11" in done.stderr

    def test_its_own_failure_codes_are_not_retried(self, tmp_path):
        """Exit 3 is "no metadata"; repeating it repeats a known failure."""
        counter = tmp_path / "runs"
        script = fake_serve(tmp_path, "echo run >> %s\nexit 3\n" % counter)
        done = run_wrapper(tmp_path, script)
        assert done.returncode == 3
        assert counter.read_text().count("run") == 1

    def test_a_normal_end_is_not_retried(self, tmp_path):
        counter = tmp_path / "runs"
        script = fake_serve(tmp_path, "echo run >> %s\necho served\n" % counter)
        done = run_wrapper(tmp_path, script)
        assert done.returncode == 0
        assert counter.read_text().count("run") == 1

    def test_a_second_signal_is_reported(self, tmp_path):
        script = fake_serve(tmp_path, "kill -SEGV $$\n")
        done = run_wrapper(tmp_path, script)
        assert done.returncode == 139

    def test_arguments_reach_the_child(self, tmp_path):
        script = fake_serve(tmp_path, 'printf "%s\\n" "$@"\n')
        done = run_wrapper(
            tmp_path, script, ["magnet:?xt=urn:btih:abc&tr=udp%3A%2F%2Fx", "--dir", "/d"]
        )
        assert done.stdout.splitlines() == [
            "magnet:?xt=urn:btih:abc&tr=udp%3A%2F%2Fx",
            "--dir",
            "/d",
        ]

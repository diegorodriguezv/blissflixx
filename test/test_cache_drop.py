"""
Evicting the torrent buffer's page cache.

Verifying a buffered torrent reads gigabytes through the page cache, and on
the Pi those pages land in the display memory's region faster than the kernel
migrates them out -- a play starting with a pressured region decodes to a
black screen. The verifier reads each piece once and never again, so what has
been read is never needed in memory, and drop_buffer_cache tells the kernel
so: before the player allocates, periodically beside the verifier, and after
the play stops.

Best effort by contract: a play must not fail because housekeeping did, so a
missing directory, a missing syscall and an unreadable file all drop nothing
rather than raise.
"""

import os

import lib.player.buffer as buffer


def _write(path):
    with open(path, "wb") as f:
        f.write(os.urandom(1 << 20))


class TestDropBufferCache:
    def test_missing_directory_drops_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buffer, "BUFFER_DIR", str(tmp_path / "absent"))
        assert buffer.drop_buffer_cache() == 0

    def test_drops_every_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buffer, "BUFFER_DIR", str(tmp_path))
        _write(str(tmp_path / "a.bin"))
        sub = tmp_path / "sub"
        sub.mkdir()
        _write(str(sub / "b.bin"))
        assert buffer.drop_buffer_cache() == 2

    def test_without_the_syscall_drops_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buffer, "BUFFER_DIR", str(tmp_path))
        _write(str(tmp_path / "a.bin"))
        monkeypatch.setattr(os, "posix_fadvise", None, raising=False)
        assert buffer.drop_buffer_cache() == 0

    def test_open_failure_does_not_raise(self, tmp_path, monkeypatch):
        monkeypatch.setattr(buffer, "BUFFER_DIR", str(tmp_path))
        _write(str(tmp_path / "a.bin"))
        _write(str(tmp_path / "b.bin"))
        real_open = os.open

        def failing_open(path, *args, **kwargs):
            if os.path.basename(path) == "b.bin":
                raise OSError("gone")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", failing_open)
        assert buffer.drop_buffer_cache() == 1

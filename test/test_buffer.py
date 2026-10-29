"""
Making room in the buffer: whole entries, oldest use first.

Nothing here writes state. "Last use" is the filesystem's atime/mtime, so the
tests set those explicitly rather than pretending there is a database.
"""

import os

import lib.player.buffer as space


def _touch(path, when):
    os.utime(path, (when, when))


def _root(tmp_path):
    """A buffer directory of our own; tmp_path itself holds the settings fixture."""
    root = tmp_path / "buffer"
    root.mkdir()
    return root


def _file(path, size, when):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    _touch(path, when)
    return path


def _alloc(path):
    """What the file really occupies, which is what deleting it frees."""
    return os.stat(path).st_blocks * 512


class TestEntries:
    def test_a_file_is_one_entry(self, tmp_path):
        root = _root(tmp_path)
        old = _file(root / "old.mkv", 100, 1000)
        found = space.entries(str(root))
        assert [e["name"] for e in found] == ["old.mkv"]
        assert found[0]["size"] == _alloc(old)
        assert found[0]["last"] == 1000

    def test_a_pack_is_one_entry_and_uses_its_newest_file(self, tmp_path):
        root = _root(tmp_path)
        pack = root / "Pack"
        e01 = _file(pack / "e01.mkv", 100, 1000)
        e02 = _file(pack / "e02.mkv", 200, 3000)
        # The directory's own time is old; the last episode watched is what
        # makes the pack recent. Its atime is deliberately not used: the scan
        # itself lists the directory and would refresh it.
        _touch(pack, 500)
        found = space.entries(str(root))
        assert len(found) == 1
        assert found[0]["name"] == "Pack"
        assert found[0]["size"] == _alloc(e01) + _alloc(e02)
        assert found[0]["last"] == 3000

    def test_a_sparse_file_counts_only_allocated_blocks(self, tmp_path):
        """A 41GB remux with 2GB of pieces must not claim to free 41GB."""
        root = _root(tmp_path)
        sparse = root / "sparse.mkv"
        with sparse.open("wb") as handle:
            handle.truncate(10**9)
        _touch(sparse, 1000)
        found = space.entries(str(root))[0]
        assert found["size"] < 1024 * 1024

    def test_hidden_internal_directories_are_not_films(self, tmp_path):
        root = _root(tmp_path)
        _file(root / ".bf-verified" / "abc.json", 10, 1000)
        _file(root / "film.mkv", 100, 2000)
        assert [e["name"] for e in space.entries(str(root))] == ["film.mkv"]


class TestReclaim:
    def test_the_oldest_entry_goes_first(self, tmp_path):
        root = _root(tmp_path)
        old = _file(root / "old.mkv", 100, 1000)
        old_size = _alloc(old)
        _file(root / "new.mkv", 100, 2000)
        freed, removed, free = space.reclaim(str(root), needed=old_size, free=0)
        assert freed == old_size
        assert removed == ["old.mkv"]
        assert free == 0
        assert not (root / "old.mkv").exists()
        assert (root / "new.mkv").exists()

    def test_it_stops_once_enough_is_free(self, tmp_path):
        root = _root(tmp_path)
        old = _file(root / "old.mkv", 100, 1000)
        old_size = _alloc(old)
        _file(root / "new.mkv", 100, 2000)
        freed, removed, _ = space.reclaim(str(root), needed=old_size, free=0)
        assert removed == ["old.mkv"]
        assert (root / "new.mkv").exists()
        assert freed >= old_size

    def test_a_pack_is_deleted_as_a_whole(self, tmp_path):
        root = _root(tmp_path)
        pack = root / "Pack"
        e01 = _file(pack / "e01.mkv", 100, 1000)
        e02 = _file(pack / "e02.mkv", 200, 2000)
        pack_size = _alloc(e01) + _alloc(e02)
        freed, removed, _ = space.reclaim(str(root), needed=pack_size, free=0)
        assert removed == ["Pack"]
        assert freed == pack_size
        assert not pack.exists()

    def test_the_current_entry_is_never_deleted(self, tmp_path):
        root = _root(tmp_path)
        _file(root / "current.mkv", 100, 1000)
        other = _file(root / "other.mkv", 200, 2000)
        other_size = _alloc(other)
        freed, removed, _ = space.reclaim(
            str(root), needed=10**9, current_entry="current.mkv", free=0
        )
        assert removed == ["other.mkv"]
        assert freed == other_size
        assert (root / "current.mkv").exists()
        assert not (root / "other.mkv").exists()

    def test_nothing_to_do_when_there_is_already_room(self, tmp_path):
        root = _root(tmp_path)
        _file(root / "film.mkv", 100, 1000)
        freed, removed, free = space.reclaim(str(root), needed=100, free=100)
        assert (freed, removed, free) == (0, [], 100)
        assert (root / "film.mkv").exists()

    def test_with_only_the_current_entry_it_removes_nothing(self, tmp_path):
        root = _root(tmp_path)
        _file(root / "current.mkv", 100, 1000)
        freed, removed, _ = space.reclaim(
            str(root), needed=10**9, current_entry="current.mkv", free=0
        )
        assert (freed, removed) == (0, [])
        assert (root / "current.mkv").exists()

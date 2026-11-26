"""
The torrent buffer: where downloads live, how space is made for one, and how
its pages are kept out of memory the display needs.

Space: make room for a play by deleting the least recently used buffer entry.
Deliberately small and stateless. "Last used" is the filesystem's own record:
the newest of a file's atime and mtime, or, for a pack directory, the newest of
the directory's mtime and its files' atimes and mtimes. Nothing is written down
and nothing survives in memory between plays.

A top-level entry is the unit: one film file, or one pack directory. Complete
and incomplete downloads are treated alike -- only one torrent downloads at a
time, so another partial entry is dormant -- and the entry belonging to the
play that asked for the space is never a candidate.

The policy is a single decision: free the bytes still needed for this film plus
HEADROOM, at the moment the film's size is known, and never look again.
"""

import os
import shutil

from ..locations import DATA_PATH

#: Left for the system after the film has finished downloading.
HEADROOM = 100 * 1024 * 1024

#: Where a torrent's files are written, and where the space manager looks. The
#: torrent process is told this directory; it does not choose one.
#:
#: On the SD card, not in /tmp. On any current Pi /tmp is a tmpfs sized at half
#: of RAM (systemd's static tmp.mount, Options=...,size=50%%), and a 1080p film
#: is 2-4GB against 371MB: a download there cannot finish, and the writes after
#: it fails fill the mount and fail with ENOSPC, which is how the on-screen
#: confirmation once stopped appearing.
BUFFER_DIR = os.path.join(DATA_PATH, "downloads", "torrent-stream")


def drop_file_cache(path):
    """
    Evict one file's pages, or say zero. Never raises.

    The single-file form of drop_buffer_cache, for a film being read from the
    card: VLC reads its own media through the same page cache, and those pages
    land in the display memory's region just as the verifier's do. Measured on
    rpi with a 1080p file, the same play failed at 0:30 without this and ran
    past 1:35 with it, CmaFree steady at 64MB either way until the failure.
    """
    try:
        fadvise = getattr(os, "posix_fadvise", None)
        dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
        if fadvise is None or dontneed is None or not path:
            return 0
        fd = os.open(path, os.O_RDONLY)
        try:
            fadvise(fd, 0, 0, dontneed)
        finally:
            os.close(fd)
        return 1
    except OSError:
        return 0


def drop_buffer_cache():
    """
    Evict the torrent buffer's file pages from memory. Returns how many files
    were dropped.

    A hash check reads gigabytes through the page cache, and
    on this box those pages land in the display memory's region faster than
    the kernel migrates them out: measured on rpi, a 60MB read cost 155MB of
    CmaFree, and a play starting with a pressured region decodes to a black
    screen -- "Atomic commit failed: No space left on device", which is the
    display, not the disk. A check reads each piece once and never again, so
    what has been read is never needed in memory; telling the kernel so
    keeps the region defragmented for the decoder's large contiguous
    allocations. Needs no privileges: an eviction hint on files this user can
    read.

    Best effort, so it never raises: a play must not fail because housekeeping
    did.
    """
    try:
        return _drop_buffer_cache()
    except Exception:
        return 0


def _drop_buffer_cache():
    if not os.path.isdir(BUFFER_DIR):
        return 0
    fadvise = getattr(os, "posix_fadvise", None)
    dontneed = getattr(os, "POSIX_FADV_DONTNEED", None)
    if fadvise is None or dontneed is None:
        return 0
    dropped = 0
    for root, _dirs, files in os.walk(BUFFER_DIR):
        for name in files:
            try:
                fd = os.open(os.path.join(root, name), os.O_RDONLY)
            except OSError:
                continue
            try:
                fadvise(fd, 0, 0, dontneed)
                dropped += 1
            except OSError:
                pass
            finally:
                os.close(fd)
    return dropped


def free_bytes(root):
    """Free space on the filesystem holding `root`, for an unprivileged user."""
    st = os.statvfs(root)
    return st.f_bavail * st.f_frsize


def _allocated(st):
    """The bytes this file really occupies, not its logical length."""
    return getattr(st, "st_blocks", 0) * 512


def _measure(path):
    """Allocated size and last-use time of one top-level entry, or None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if os.path.isfile(path):
        return _allocated(st), max(st.st_atime, st.st_mtime)

    # A directory is a pack, and one unit. Its own atime is deliberately left
    # out: merely listing it here updates that atime, which would make every
    # scan mark every pack as freshly used and destroy the order. Its mtime
    # still says when something was last written or removed.
    size = 0
    last = st.st_mtime
    for root, _dirs, names in os.walk(path):
        for name in names:
            child = os.path.join(root, name)
            try:
                cst = os.stat(child)
            except OSError:
                continue
            size += _allocated(cst)
            use = max(cst.st_atime, cst.st_mtime)
            if use > last:
                last = use
    return size, last


def entries(root):
    """Every top-level entry that may be deleted: name, path, size, last use."""
    found = []
    try:
        names = os.listdir(root)
    except OSError:
        return found
    for name in names:
        # .bf-verified and anything else internal is not a film.
        if name.startswith("."):
            continue
        path = os.path.join(root, name)
        measured = _measure(path)
        if measured is None:
            continue
        size, last = measured
        found.append({"name": name, "path": path, "size": size, "last": last})
    return found


def reclaim(root, needed, current_entry=None, free=None):
    """
    Delete least-recently-used entries until `needed` bytes are free.

    `needed` is the free space wanted after the deletions, not the number of
    bytes to remove: the caller passes the film's remaining bytes plus the
    headroom it wants left. The entry named `current_entry` -- the one this play
    is using -- is never a candidate.

    Returns (freed, removed_names, free_before). Nothing is persisted; an
    interrupted deletion is simply a smaller entry next time.
    """
    if free is None:
        try:
            free = free_bytes(root)
        except OSError:
            return 0, [], 0
    if free >= needed:
        return 0, [], free

    candidates = [e for e in entries(root) if e["name"] != current_entry]
    # Oldest first; among equally old entries, the larger one, so one deletion
    # is more likely to be enough.
    candidates.sort(key=lambda e: (e["last"], -e["size"]))

    freed = 0
    removed = []
    for entry in candidates:
        if free + freed >= needed:
            break
        try:
            if os.path.isdir(entry["path"]):
                shutil.rmtree(entry["path"])
            else:
                os.remove(entry["path"])
        except OSError:
            continue
        freed += entry["size"]
        removed.append(entry["name"])
    return freed, removed, free

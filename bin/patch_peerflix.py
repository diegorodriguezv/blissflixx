#!/usr/bin/env python3
"""
Start peerflix's server when the torrent is known, not when it is verified.

torrent-stream hashes every piece already on disk before it emits its first
events, so a film that has been watched has to be hashed from end to end before
the player may ask for a single byte. Measured on rpi: 249 seconds for a 2.3GB
buffer, against a card that reads at 11.8 MB/s -- and the pieces themselves
become usable one at a time, in order, as the hashing reaches them.

So peerflix is told to serve as soon as the file list exists. The player starts,
reads from the beginning, and the hashing runs ahead of it: 11.8 MB/s against a
stream's 1 MB/s means it stays minutes ahead of the picture even on that card.
A partially buffered film starts on what is verified and continues when the
swarm picks up, which is a start in seconds instead of a black screen for four
minutes.

Two edits, both marked so a second run is a no-op:

  torrent-stream/index.js   emit 'torrent' as soon as the files are built
  peerflix/index.js         set the server up and listen on 'torrent'

peerflix is installed by configure.sh, so this runs from there. It is safe to
run by hand on a box that already has it.
"""

import pathlib
import sys

MARK = "blissflixx:serve-before-verify"

#: The second half of the same idea, under its own marker: opening the port at
#: metadata is not enough on its own. The piece a read asks for first may be the
#: one being hashed at that moment, and with nothing yet asking the swarm for
#: it, the read blocks until the hashing is done -- measured, 250 seconds with
#: the port open and the picture late. So the swarm and the picker start at the
#: same moment as the port, and a read for an unverified piece is fetched rather
#: than waited on.
SWARM_MARK = "blissflixx:swarm-before-verify"

TREE = pathlib.Path("/usr/local/lib/node_modules/peerflix")

TSTREAM = TREE / "node_modules" / "torrent-stream" / "index.js"
PEERFLIX = TREE / "index.js"
APP = TREE / "app.js"

#: The end of the file list, which is the first moment either the server or the
#: player can be told which files exist.
FILES_END = """      return file
    })
"""

EARLY_EMIT = (
    """      return file
    })

    // %s -- the file list and the piece hashes are known now, so the player may
    // start serving what is already verified while the rest is still hashed.
    engine.emit('torrent', torrent)
"""
    % MARK
)

LATE_EMIT = "        engine.emit('torrent', torrent)\n"
LATE_EMIT_OFF = "        // (%s) emitted with the file list, not here.\n" % MARK

SETUP_ON_READY = "  else e.on('ready', onready)\n"
SETUP_ON_TORRENT = "  else e.on('torrent', onready) // %s\n" % MARK

LISTEN_ON_READY = """  // Listen when torrent-stream is ready, by default a random port.
  engine.on('ready', function () {
    engine.server.listen(opts.port || 0, opts.hostname)
  })
"""
LISTEN_ON_TORRENT = (
    """  // %s -- listen as soon as the torrent is known. Waiting for 'ready' means
  // waiting for every byte already on disk to be hashed first, which is minutes
  // for a film watched before; the pieces are verified one at a time from the
  // start, so a player reading from the start is never far behind.
  engine.on('torrent', function () {
    engine.server.listen(opts.port || 0, opts.hostname)
  })
"""
    % MARK
)


def patch(path, pairs, mark=MARK):
    if not path.exists():
        print("  %s: not there" % path)
        return False
    text = path.read_text()
    if mark in text:
        print("  %s: already patched" % path.name)
        return False
    for old, new in pairs:
        if text.count(old) != 1:
            print(
                "  %s: %r appears %d times, not patching"
                % (path.name, old[:40], text.count(old))
            )
            return False
        text = text.replace(old, new, 1)
    path.write_text(text)
    print("  %s: patched" % path.name)
    return True


VERIFY_LOOP = """    if (opts.verify === false) return onready()

    engine.emit('verifying')

    var loop = function (i) {
      if (i >= torrent.pieces.length) return onready()
"""

VERIFY_LOOP_EARLY = (
    """    if (opts.verify === false) return onready()

    // %s -- the swarm and the picker start with the file list, not after the
    // hashing. Without this the port opens and the first read still blocks: the
    // piece it wants is being verified at that moment, and nothing is asking
    // the swarm for it yet.
    onready()

    engine.emit('verifying')

    var loop = function (i) {
      if (i >= torrent.pieces.length) return
"""
    % SWARM_MARK
)

STOP_VERIFYING = """  engine.on('verifying', startShowVerifying)
  engine.on('ready', stopShowVerifying)
"""

KEEP_VERIFYING = (
    """  // %s -- 'ready' now means the file list is known, which is before the
  // hashing. The percentage keeps being printed while the film plays.
  engine.on('verifying', startShowVerifying)
"""
    % SWARM_MARK
)


def main():
    if not TREE.exists():
        print("peerflix is not installed at %s" % TREE)
        return 1
    print("patching peerflix to serve before verifying:")
    patch(TSTREAM, [(FILES_END, EARLY_EMIT), (LATE_EMIT, LATE_EMIT_OFF)])
    patch(
        PEERFLIX,
        [(SETUP_ON_READY, SETUP_ON_TORRENT), (LISTEN_ON_READY, LISTEN_ON_TORRENT)],
    )
    print("and to start the swarm with it:")
    patch(TSTREAM, [(VERIFY_LOOP, VERIFY_LOOP_EARLY)], mark=SWARM_MARK)
    patch(APP, [(STOP_VERIFYING, KEEP_VERIFYING)], mark=SWARM_MARK)
    return 0


if __name__ == "__main__":
    sys.exit(main())

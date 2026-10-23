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

A third marker remembers verification across runs: .bf-verified/<infohash>.json
beside the buffer, taken back instead of re-hashed while the files are still
there at the same sizes. See CACHE_MARK below.

peerflix is installed by configure.sh, so this runs from there. It is safe to
run by hand on a box that already has it.
"""

import os
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

#: The third part: remember the answer. The loop below hashes every piece
#: already on disk on every start -- measured, 195 seconds for a 2.3GB buffer
#: that has not changed since the last play -- and keeps the result only in
#: memory. So the finished bitfield is written to .bf-verified/<infohash>.json
#: beside the buffer, and the next start with the same files present at the
#: same sizes takes it back instead of hashing. Trusted, not re-checked: files
#: here are written once by peerflix and never touched afterwards, so a
#: bitfield recorded at loop end is still true; the only check is presence, so
#: a file the user deleted cannot keep its pieces marked have. A hit prints
#: one line and emits no verifying/verify events, because pflixproc never
#: clears its VERIFYING state and the bar would be stuck on it all film.
CACHE_MARK = "blissflixx:verify-cache"

#: Skip the read without reinstalling: BF_VERIFY_CACHE=0 hashes every time.
CACHE_OFF = "BF_VERIFY_CACHE"

#: Trees patched before the save tolerated missing files carry a save that
#: throws away the whole flag on a partial torrent (statSync on a file that
#: is not there), so no flag is ever written. The upgrade below fixes those
#: two hunks in place; fresh trees already have the fixed text.
CACHE_FIX_MARK = "blissflixx:verify-cache-tolerates-missing"

#: The two hunks as first deployed, byte for byte.
BUGGY_LOAD_CHECK = """      for (i = 0; i < torrent.files.length; i++) {
        var st
        try {
          st = fs.statSync(path.join(opts.path, torrent.files[i].path))
        } catch (e) { return false }
        if (!st.isFile() || st.size !== cached.files[i].size) return false
      }
"""

BUGGY_SAVE_SIZES = """          files: torrent.files.map(function (f) {
            return {
              path: f.path,
              size: fs.statSync(path.join(opts.path, f.path)).size
            }
          })
"""

BUGGY_SAVE_DEF = """    var bfVerifyCacheSave = function () {
"""

#: Where npm put it. Overridable so the patch can be tried on a copy first.
TREE = pathlib.Path(
    os.environ.get("BF_PEERFLIX_TREE", "/usr/local/lib/node_modules/peerflix")
)

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

#: The verification loop as the swarm patch leaves it: the swarm already
#: started, the port already listening, pieces hashed one at a time in order.
VERIFY_CACHED_LOOP = """    engine.emit('verifying')

    var loop = function (i) {
      if (i >= torrent.pieces.length) return
      engine.store.get(i, function (_, buf) {
        if (!buf || sha1(buf) !== torrent.pieces[i] || !pieces[i]) return loop(i + 1)
        pieces[i] = null
        engine.bitfield.set(i, true)
        engine.emit('verify', i)
        loop(i + 1)
      })
    }

    loop(0)
"""

VERIFY_CACHED_LOOP_NEW = (
    """    // %s -- a previous run already hashed every piece on disk and wrote
    // the answer down, so take it back instead of hashing again. Trusted, not
    // re-checked: files here are written once and never touched afterwards.
    // The only check is presence, by index and size -- a file that is absent
    // is recorded as -1, so a deleted file cannot keep its pieces marked
    // have, and a file that arrived since is correctly refused. Restores the picker too, not just the
    // bitfield: selections advance on pieces[i] going null, so a have piece
    // left non-null would be fetched from the swarm all over again.
    var bfVerifyCachePath = function () {
      return path.join(opts.path, '.bf-verified', infoHash + '.json')
    }

    var bfVerifyCacheLoad = function () {
      var cached
      try {
        cached = JSON.parse(fs.readFileSync(bfVerifyCachePath()))
      } catch (e) { return false }
      if (!cached || cached.infoHash !== infoHash) return false
      if (cached.pieces !== torrent.pieces.length) return false
      if (!cached.files || cached.files.length !== torrent.files.length) return false
      var i
      for (i = 0; i < torrent.files.length; i++) {
        var st
        try {
          st = fs.statSync(path.join(opts.path, torrent.files[i].path))
        } catch (e) { st = null }
        var actual = (st && st.isFile()) ? st.size : -1
        if (actual !== cached.files[i].size) return false
      }
      var have
      try {
        have = bitfield(Buffer.from(cached.bitfield, 'base64'))
      } catch (e) { return false }
      if (have.buffer.length !== engine.bitfield.buffer.length) return false
      for (i = 0; i < torrent.pieces.length; i++) {
        if (have.get(i)) pieces[i] = null
      }
      engine.bitfield = have
      return true
    }

    // blissflixx:verify-cache-tolerates-missing -- save/load tolerate files
    // that are not on disk (a partial torrent): absent records -1, so the
    // flag is written and refused on presence rather than crashing the save.
    var bfVerifyCacheSize = function (rel) {
      try {
        return fs.statSync(path.join(opts.path, rel)).size
      } catch (e) { return -1 }
    }

    var bfVerifyCacheSave = function () {
      try {
        fs.mkdirSync(path.dirname(bfVerifyCachePath()), { recursive: true })
        fs.writeFileSync(bfVerifyCachePath(), JSON.stringify({
          infoHash: infoHash,
          pieces: torrent.pieces.length,
          bitfield: engine.bitfield.buffer.toString('base64'),
          files: torrent.files.map(function (f) {
            return {
              path: f.path,
              size: bfVerifyCacheSize(f.path)
            }
          })
        }))
      } catch (e) { /* best effort: no flag just means hashing again */ }
    }

    if (process.env.%s !== '0' && bfVerifyCacheLoad()) {
      console.log('bf-verify-cache: hit ' + infoHash)
      return
    }

    engine.emit('verifying')

    var loop = function (i) {
      if (i >= torrent.pieces.length) return bfVerifyCacheSave()
      engine.store.get(i, function (_, buf) {
        if (!buf || sha1(buf) !== torrent.pieces[i] || !pieces[i]) return loop(i + 1)
        pieces[i] = null
        engine.bitfield.set(i, true)
        engine.emit('verify', i)
        loop(i + 1)
      })
    }

    loop(0)
"""
    % (CACHE_MARK, CACHE_OFF)
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
    print("and to remember verified pieces across runs:")
    try:
        before_cache = TSTREAM.read_text()
    except OSError:
        before_cache = ""
    patch(TSTREAM, [(VERIFY_CACHED_LOOP, VERIFY_CACHED_LOOP_NEW)], mark=CACHE_MARK)
    if CACHE_MARK in before_cache and CACHE_FIX_MARK not in before_cache:
        print("and to tolerate missing files in the flag:")
        patch(
            TSTREAM,
            [
                (
                    BUGGY_LOAD_CHECK,
                    """      for (i = 0; i < torrent.files.length; i++) {
        var st
        try {
          st = fs.statSync(path.join(opts.path, torrent.files[i].path))
        } catch (e) { st = null }
        var actual = (st && st.isFile()) ? st.size : -1
        if (actual !== cached.files[i].size) return false
      }
""",
                ),
                (
                    BUGGY_SAVE_SIZES,
                    """          files: torrent.files.map(function (f) {
            return {
              path: f.path,
              size: bfVerifyCacheSize(f.path)
            }
          })
""",
                ),
                (
                    BUGGY_SAVE_DEF,
                    """    // blissflixx:verify-cache-tolerates-missing -- save/load tolerate files
    // that are not on disk (a partial torrent): absent records -1, so the
    // flag is written and refused on presence rather than crashing the save.
    var bfVerifyCacheSize = function (rel) {
      try {
        return fs.statSync(path.join(opts.path, rel)).size
      } catch (e) { return -1 }
    }

    var bfVerifyCacheSave = function () {
""",
                ),
            ],
            mark=CACHE_FIX_MARK,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

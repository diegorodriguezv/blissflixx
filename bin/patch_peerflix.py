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

A fourth wakes reads as pieces verify, in lib/file-stream.js: a read for an
unverified piece parks on engine.critical() with nothing waking it, because
selections advance through gc() and the verify loop never calls it. See
WAKE_MARK below.

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

#: Trace what the player asks for and where a stream stops.
#:
#: Two lines, because the failures this box has been chasing are invisible
#: from both ends: VLC reports "end of playlist" with nothing before it, and
#: peerflix prints only its verification percent. With these, the log says
#: which range was requested, what was answered, and when a read stream
#: decided it had finished -- which is the difference between "the player
#: stopped asking" and "the server stopped answering".
TRACE_MARK = "blissflixx:trace-requests"

#: What discovery is doing, so "0 peers" can be read rather than guessed.
#:
#: peerflix prints a peer count and nothing else: a tracker that refused, a
#: DHT that never started, and a swarm with nobody in it all look the same from
#: the outside. Two lines fix that -- one per tracker announce, one every
#: fifteen seconds with the DHT and swarm state.
DISCOVERY_MARK = "blissflixx:discovery-log"

#: A replay from the .torrent cache asked no tracker at all.
#:
#: The metadata fetched from the swarm is saved with 'announce-list': [], so
#: parsing it back gives a torrent with no announce list, and discovery is
#: built from that torrent -- zero trackers. A fresh play, whose metadata is
#: not on disk yet, is built from the magnet and does have them, which is why
#: "0 peers" happened on replays of films that worked the first time. The
#: magnet's announce list is the only surviving record of where to look, so it
#: is merged into every torrent handed to discovery.
KEEP_TRACKERS_MARK = "blissflixx:keep-magnet-trackers"

#: The DHT is asked for peers before it has finished bootstrapping.
#:
#: bittorrent-dht answers "No nodes to query" on that first announce and
#: discovery waits its normal interval -- a quarter of an hour -- before
#: asking again. Retrying in fifteen seconds until it answers costs nothing
#: and turns the DHT from something that may help later into a peer source
#: for the first read.
DHT_RETRY_MARK = "blissflixx:dht-retry-until-bootstrapped"

#: How many trackers discovery actually has, in the state line.
SWARM_TRACKERS_MARK = "blissflixx:swarm-tracker-count"

#: torrent-discovery 5.4.0's announce, verbatim.
DHT_ANNOUNCE = """  self.dht.announce(self.infoHash, self.port, function (err) {
    self._dhtAnnouncing = false
    debug('dht announce complete')

    if (err) self.emit('warning', err)
    self.emit('dhtAnnounce')

    if (!self.destroyed) {
      self._dhtTimeout = setTimeout(function () {
        self._dhtAnnounce()
      }, getRandomTimeout())
    }
  })
"""

DHT_ANNOUNCE_NEW = """  self.dht.announce(self.infoHash, self.port, function (err) {
    self._dhtAnnouncing = false
    debug('dht announce complete')

    if (err) {
      // %s -- "No nodes to query" is the DHT before it has bootstrapped, not
      // an empty swarm. Waiting the normal interval leaves discovery blind
      // for a quarter of an hour, so retry soon until it answers.
      self.emit('warning', err)
      if (!self.destroyed) {
        self._dhtTimeout = setTimeout(function () {
          self._dhtAnnounce()
        }, 15000)
        return
      }
    }

    self.emit('dhtAnnounce')

    if (!self.destroyed) {
      self._dhtTimeout = setTimeout(function () {
        self._dhtAnnounce()
      }, getRandomTimeout())
    }
  })
""" % DHT_RETRY_MARK

#: A range that cannot be satisfied must be answered 416, not with the file.
#:
#: VLC asks "Range: bytes=<length>-" -- a request that starts exactly at the
#: end of the file -- to learn how large the stream is. A server that answers
#: 416 with "Content-Range: bytes */<size>" has told it, and VLC goes on to
#: seek where it needs (an MP4's index, which this box's films often keep at
#: the end). peerflix passed the unsatisfiable range to range-parser, got -1,
#: and fell through to the "no range" branch: it answered 200 with the entire
#: file. VLC then concluded the stream could not be seeked at all, and read it
#: linearly instead -- 320MB of a 3GB film, waiting for an index it would
#: reach in half an hour, while the download ran on. Measured on rpi with two
#: fresh films; both stalled the same way, both had asked for bytes=<length>-.
RANGE_MARK = "blissflixx:unsatisfiable-range-416"

#: A read that is parked on a piece the swarm does not have yet takes priority
#: over the download's own order. Measured on rpi: a 5.3GB MP4 whose index sits
#: at the end of the file -- VLC asks for "bytes=5314433909-" before it will
#: demux anything -- and peerflix answered only when its sequential download
#: reached that offset, hours later. VLC read 666MB of a film it could not
#: start and showed a black screen. The range a reader is waiting on is now
#: moved to the front of the engine's selections, so its pieces are fetched in
#: seconds; the download resumes its own order afterwards.
PARKED_MARK = "blissflixx:parked-read-priority"

#: A store error on a read is retried rather than ending the response; see
#: FSTREAM_ERROR below.
STREAM_MARK = "blissflixx:stream-survives-read-error"

#: The retry timer added to that patch after rpitv showed why the event alone
#: is not enough; trees patched before it get the block replaced in place.
STREAM_RETRY_MARK = "blissflixx:stream-retry-timer"

#: Where a read stream ends, said out loud. See TRACE_MARK.
STREAM_END_MARK = "blissflixx:trace-stream-end"

#: The cache only remembers a complete torrent, and says so with a version so
#: that flags written before this rule are ignored rather than trusted. Trees
#: installed before it get the three hunks below.
CACHE_V2_MARK = "blissflixx:verify-cache-complete-only"

#: A piece the cached bitfield calls missing is hashed before the read waits
#: for the swarm. See the parked branch below: without it, a bitfield written
#: before the data arrived refuses bytes that are on the disk, and the old cure
#: -- refusing to cache partial torrents -- cost three minutes of re-hashing on
#: every play of a 99 per cent complete film.
LOOK_MARK = "blissflixx:look-at-the-piece"

#: A cached file that has grown is still a valid cache; one that shrank is not.
CACHE_GROWTH_MARK = "blissflixx:verify-cache-accepts-growth"

#: _look() must never throw; see the body for why. Trees that got the first
#: version get the safe one under this marker.
LOOK_FIX_MARK = "blissflixx:look-never-throws"

#: The first version's callback, as installed.
LOOK_OLD_BODY = """    this._engine.store.get(index, function (err, buffer) {
      if (err || !buffer) {
        self._engine.critical(index, self._critical)
        return
      }
      var hash = require('crypto').createHash('sha1').update(buffer).digest()
      var want = self._engine.torrent && self._engine.torrent.pieces[index]
      if (!want || !hash.equals(want)) {
        self._engine.critical(index, self._critical)
        return
      }
      if (typeof console !== 'undefined') console.log('bf-stream: piece ' + index + ' was on disk after all')
      self._engine.bitfield.set(index, true)
      self.notify()
    })"""

LOOK_SAFE_BODY = """    this._engine.store.get(index, function (err, buffer) {
      // %s -- never throw from here: it runs on a read the player is waiting
      // for, and an exception takes the whole server down. It did, comparing
      // the hash against a piece hash that is not a Buffer, and the player was
      // left spinning against a dead stream.
      try {
        if (err || !buffer) {
          self._engine.critical(index, self._critical)
          return
        }
        var hash = require('crypto').createHash('sha1').update(buffer).digest('hex')
        var want = self._engine.torrent && self._engine.torrent.pieces[index]
        if (!want || hash !== Buffer.from(want).toString('hex')) {
          self._engine.critical(index, self._critical)
          return
        }
        if (typeof console !== 'undefined') console.log('bf-stream: piece ' + index + ' was on disk after all')
        self._engine.bitfield.set(index, true)
        self.notify()
      } catch (e) {
        if (typeof console !== 'undefined') console.log('bf-stream: look failed for piece ' + index + ': ' + e)
        self._engine.critical(index, self._critical)
      }
    })""" % LOOK_FIX_MARK

#: Waking reads as pieces verify. Serving before verifying exposed a race the
#: stock order never hit: a read for an unverified piece parks on
#: engine.critical() and nothing wakes it, because selections advance through
#: gc() and the verify loop never calls it. Upstream always hashed everything
#: before listening, so the first read never parked; here the player connects
#: at once, and without a wakeup it waits until it re-requests on its own --
#: measured, five silent minutes with the port open and the bytes on disk.
#: 'verify' fires for loop verifications and swarm downloads alike, so one
#: subscription wakes both.
WAKE_MARK = "blissflixx:verify-wakeup"

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
FSTREAM = TREE / "node_modules" / "torrent-stream" / "lib" / "file-stream.js"
TDISCOVERY = TREE / "node_modules" / "torrent-discovery" / "index.js"

#: The end of the file list, which is the first moment either the server or the
#: player can be told which files exist.
FILES_END = """      return file
    })
"""

#: The constructor's last line, and destroy: the only two places a stream's
#: lifetime is managed.
FSTREAM_CTOR = """  this._offset = offset - this.startPiece * pieceLength
}
"""

FSTREAM_CTOR_NEW = (
    """  this._offset = offset - this.startPiece * pieceLength
  // %s -- re-drive this stream as pieces verify. A read for an unverified
  // piece parks on engine.critical() with nothing waking it afterwards, so
  // subscribe to the event both verifications and downloads emit.
  this._onverify = this.notify.bind(this)
  engine.on('verify', this._onverify)
  // %s -- one piece at a time, at most every two seconds, so a read parked on
  // something genuinely absent does not re-hash it on every retry.
  this._look = function () {
    var self = this
    var index = this._piece
    var now = Date.now()
    this._looked = this._looked || {}
    if (this._looked[index] && now - this._looked[index] < 2000) return
    this._looked[index] = now
    this._engine.store.get(index, function (err, buffer) {
      // Never let this path throw: it runs on a read the player is waiting
      // for, and an exception here takes the whole server down. It did --
      // "The otherBuffer argument must be an instance of Buffer", from
      // comparing the hash against a piece hash that is not a Buffer -- and
      // the player was left spinning at full CPU against a dead stream.
      try {
        if (err || !buffer) {
          self._engine.critical(index, self._critical)
          return
        }
        var hash = require('crypto').createHash('sha1').update(buffer).digest('hex')
        var want = self._engine.torrent && self._engine.torrent.pieces[index]
        if (!want || hash !== Buffer.from(want).toString('hex')) {
          self._engine.critical(index, self._critical)
          return
        }
        if (typeof console !== 'undefined') console.log('bf-stream: piece ' + index + ' was on disk after all')
        self._engine.bitfield.set(index, true)
        self.notify()
      } catch (e) {
        if (typeof console !== 'undefined') console.log('bf-stream: look failed for piece ' + index + ': ' + e)
        self._engine.critical(index, self._critical)
      }
    })
  }

  // %s -- a read waiting on a piece takes the front of the queue. Without
  // this its range is just one more selection, and the download's own order
  // wins: measured, a 5.3GB MP4 whose index is at the end of the file left
  // VLC reading 666MB of a film it could not start.
  this._promote = function () {
    var selections = engine.selection
    for (var k = 1; k < selections.length; k++) {
      if (selections[k].from === this.startPiece && selections[k].to === this.endPiece) {
        selections.unshift(selections.splice(k, 1)[0])
        return
      }
    }
  }
}
"""
    % (WAKE_MARK, LOOK_MARK, PARKED_MARK)
)

#: The parked branch of notify(), which is where a reader discovers that the
#: piece it wants is not there.
#: A failed piece read must not end the stream.
#:
#: file-stream answers a store error by destroying itself, which closes the
#: HTTP response; VLC reads a closed response as the end of the film and, with
#: --play-and-exit, stops. Measured on rpi watching a torrent at 3%: the
#: picture played 43 seconds, VLC said "end of playlist, exiting" with no error
#: before it, and the app reported nothing playing. A piece that cannot be
#: read yet is not the end of a film -- wait for the next verify event and ask
#: again, which is what the same stream does when a piece is simply missing.
FSTREAM_ERROR = """    if (err) return self.destroy(err)
"""

FSTREAM_ERROR_NEW = (
    """    if (err) {
      // %s -- retry instead of closing the response.
      //
      // Twice over: on the next verify event, and on a timer. The event alone
      // is not enough -- a piece can already have been verified when the read
      // fails for another reason ("Not opened" while the store is still
      // starting), and then no event ever comes for it and the read waits for
      // ever. Measured on rpitv: a first play sat sixteen minutes with the
      // picture never arriving, and the same play after a stop started in
      // twenty seconds.
      self._piece--
      if (typeof console !== 'undefined') console.log('bf-stream: read failed for piece ' + self._piece + ': ' + err)
      self._engine.critical(self._piece, self._critical)
      setTimeout(self._onverify, 250)
      return
    }
"""
    % STREAM_MARK
)

#: The first version of the block above, as installed, for the upgrade.
FSTREAM_ERROR_OLD = """    if (err) {
      // %s -- retry when the piece arrives instead of closing the response.
      self._piece--
      if (typeof console !== 'undefined') console.log('bf-stream: read failed for piece ' + self._piece + ': ' + err)
      self._engine.critical(self._piece, self._critical)
      return
    }
""" % STREAM_MARK

FSTREAM_PARK = """  if (!this._engine.bitfield.get(this._piece)) return this._engine.critical(this._piece, this._critical)
"""

FSTREAM_PARK_NEW = (
    """  if (!this._engine.bitfield.get(this._piece)) {
    // %s -- the piece this read is waiting on is fetched ahead of the
    // download's own order; see _promote.
    this._promote()
    // %s -- and before waiting for anyone, look at the piece itself. The
    // cached bitfield records what was absent when it was written, and the
    // swarm fills holes afterwards; a stale "missing" then refuses bytes that
    // are on the disk. Hashing one piece costs a third of a second, where
    // re-verifying the film to discover the same thing cost three minutes.
    this._look()
    return this._engine.critical(this._piece, this._critical)
  }
"""
    % (PARKED_MARK, LOOK_MARK)
)

FSTREAM_DESTROY = """FileStream.prototype.destroy = function () {
  if (this._destroyed) return
  this._destroyed = true
  this.emit('close')
}
"""

FSTREAM_DESTROY_NEW = (
    """FileStream.prototype.destroy = function () {
  if (this._destroyed) return
  this._destroyed = true
  // %s -- the subscription ends with the stream; the engine outlives it for
  // the whole play.
  this._engine.removeListener('verify', this._onverify)
  this.emit('close')
}
"""
    % WAKE_MARK
)

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


#: peerflix's range handling, as installed: parse, take the first range, and
#: fall through to the whole file when there is none.
RANGE_PARSE = """    var range = request.headers.range
    range = range && rangeParser(file.length, range)[0]
"""

RANGE_PARSE_NEW = (
    """    var range = request.headers.range
    if (range) {
      // %s -- see the note above. An unsatisfiable range is answered as such,
      // because the question it usually is is "how big is this file?".
      var parsed = rangeParser(file.length, range)
      if (parsed === -1 || !parsed || !parsed.length) {
        response.statusCode = 416
        response.setHeader('Content-Range', 'bytes */' + file.length)
        response.end()
        return
      }
      range = parsed[0]
    }
"""
    % RANGE_MARK
)

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
      if (!cached || cached.version !== 2) return false
      if (cached.infoHash !== infoHash) return false
      if (cached.pieces !== torrent.pieces.length) return false
      if (!cached.files || cached.files.length !== torrent.files.length) return false
      var i
      for (i = 0; i < torrent.files.length; i++) {
        var st
        try {
          st = fs.statSync(path.join(opts.path, torrent.files[i].path))
        } catch (e) { st = null }
        var actual = (st && st.isFile()) ? st.size : -1
        // Growth is expected and must not throw the cache away: a play that
        // starts while the file is still being written sees it grow, rejected
        // the cache, re-verified 1.8GB -- and that verification then wedged at
        // its first piece for an hour, which is a bug of its own. Pieces the
        // file has gained are simply absent from the bitfield, and the
        // per-piece check in file-stream vouches for them when a read needs
        // them. A file that shrank, or has gone, is a different matter: the
        // pieces recorded as present may no longer be.
        if (actual === -1 ? cached.files[i].size !== -1
                          : actual < cached.files[i].size) return false
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
        // Partial torrents may be remembered too: the bitfield's *absent*
        // entries are re-checked one at a time when a read needs them (see
        // the parked branch of file-stream), so a stale "missing" costs a
        // single piece hash instead of hashing the whole film again.
        fs.mkdirSync(path.dirname(bfVerifyCachePath()), { recursive: true })
        fs.writeFileSync(bfVerifyCachePath(), JSON.stringify({
          version: 2,
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
    print("and to wake reads as pieces verify:")
    patch(
        FSTREAM,
        [
            (FSTREAM_CTOR, FSTREAM_CTOR_NEW),
            (FSTREAM_PARK, FSTREAM_PARK_NEW),
            (FSTREAM_DESTROY, FSTREAM_DESTROY_NEW),
        ],
        mark=WAKE_MARK,
    )
    print("and to answer an impossible range as impossible:")
    patch(PEERFLIX, [(RANGE_PARSE, RANGE_PARSE_NEW)], mark=RANGE_MARK)
    print("and to say what peer discovery is doing:")
    try:
        disc_text = TSTREAM.read_text()
    except OSError:
        disc_text = ""
    if DISCOVERY_MARK not in disc_text:
        patch(
            TSTREAM,
            [
                (
                    "  var exchange = exchangeMetadata(engine, function (metadata) {",
                    "  // %s -- one line per tracker answer, one line of state.\n"
                    "  discovery.on('trackerAnnounce', function (data) {\n"
                    "    if (typeof console === 'undefined') return\n"
                    "    var peers = (data && data.peers) ? data.peers.length : 0\n"
                    "    console.log('bf-tracker: ' + (data && data.url ? data.url : '?') +\n"
                    "      ' peers=' + peers + (data && data.failure ? ' failure=' + data.failure : ''))\n"
                    "  })\n"
                    "  discovery.on('warning', function (e) {\n"
                    "    if (typeof console !== 'undefined') console.log('bf-tracker: warning ' + (e && e.message))\n"
                    "  })\n"
                    "  discovery.on('error', function (e) {\n"
                    "    if (typeof console !== 'undefined') console.log('bf-tracker: error ' + (e && e.message))\n"
                    "  })\n"
                    "  setInterval(function () {\n"
                    "    if (typeof console === 'undefined') return\n"
                    "    console.log('bf-swarm: dht=' + (discovery.dht ? 'on' : 'off') +\n"
                    "      ' peers=' + engine.swarm.wires.length +\n"
                    "      ' downloaded=' + engine.swarm.downloaded)\n"
                    "  }, 15000).unref()\n\n"
                    "  var exchange = exchangeMetadata(engine, function (metadata) {" % DISCOVERY_MARK,
                )
            ],
            mark=DISCOVERY_MARK,
        )
    print("and to keep the magnet's trackers when metadata is replayed:")
    try:
        keep_text = TSTREAM.read_text()
    except OSError:
        keep_text = ""
    if KEEP_TRACKERS_MARK not in keep_text:
        patch(
            TSTREAM,
            [
                (
                    "  var ontorrent = function (torrent) {",
                    "  var ontorrent = function (torrent) {\n"
                    "    // %s -- metadata from the swarm is saved with an empty\n"
                    "    // announce list, so a replay from the .torrent cache was\n"
                    "    // built with no trackers and asked nobody for peers. The\n"
                    "    // magnet still names them.\n"
                    "    if (link && link.announce && link.announce.length) {\n"
                    "      torrent.announce = torrent.announce || []\n"
                    "      for (var ai = 0; ai < link.announce.length; ai++) {\n"
                    "        if (torrent.announce.indexOf(link.announce[ai]) === -1) {\n"
                    "          torrent.announce.push(link.announce[ai])\n"
                    "        }\n"
                    "      }\n"
                    "    }\n" % KEEP_TRACKERS_MARK,
                )
            ],
            mark=KEEP_TRACKERS_MARK,
        )
    print("and to retry the DHT until it has nodes:")
    if TDISCOVERY.exists():
        patch(TDISCOVERY, [(DHT_ANNOUNCE, DHT_ANNOUNCE_NEW)], mark=DHT_RETRY_MARK)
    print("and to count the trackers in the state line:")
    try:
        swarm_text = TSTREAM.read_text()
    except OSError:
        swarm_text = ""
    if SWARM_TRACKERS_MARK not in swarm_text:
        patch(
            TSTREAM,
            [
                (
                    "    console.log('bf-swarm: dht=' + (discovery.dht ? 'on' : 'off') +\n"
                    "      ' peers=' + engine.swarm.wires.length +\n"
                    "      ' downloaded=' + engine.swarm.downloaded)",
                    "    var bfTrackers = 0\n"
                    "    try { bfTrackers = discovery.tracker._trackers.length } catch (e) {}\n"
                    "    // %s -- zero here is why a replay can sit at 0 peers.\n"
                    "    console.log('bf-swarm: dht=' + (discovery.dht ? 'on' : 'off') +\n"
                    "      ' trackers=' + bfTrackers +\n"
                    "      ' peers=' + engine.swarm.wires.length +\n"
                    "      ' downloaded=' + engine.swarm.downloaded)" % SWARM_TRACKERS_MARK,
                )
            ],
            mark=SWARM_TRACKERS_MARK,
        )
    print("and to trace what the player asks for:")
    try:
        peerflix_text = PEERFLIX.read_text()
    except OSError:
        peerflix_text = ""
    if RANGE_MARK in peerflix_text and TRACE_MARK not in peerflix_text:
        patch(
            PEERFLIX,
            [
                (
                    "    var range = request.headers.range\n    if (range) {",
                    "    // %s -- what was asked for, before answering it.\n"
                    "    if (typeof console !== 'undefined') console.log('bf-http: ' + request.method + ' ' + u.pathname + ' range=' + request.headers.range)\n"
                    "    var range = request.headers.range\n    if (range) {" % TRACE_MARK,
                )
            ],
            mark=TRACE_MARK,
        )
    try:
        fstream_text = FSTREAM.read_text()
    except OSError:
        fstream_text = ""
    if WAKE_MARK in fstream_text and STREAM_END_MARK not in fstream_text:
        patch(
            FSTREAM,
            [
                (
                    "    if (!self._missing) {\n      self.push(buffer)\n      self.push(null)",
                    "    if (!self._missing) {\n"
                    "      // %s -- the stream is ending; say where and how much was asked for.\n"
                    "      if (typeof console !== 'undefined') console.log('bf-stream: end at piece ' + (self._piece - 1) + ' after ' + self.length + ' bytes')\n"
                    "      self.push(buffer)\n      self.push(null)" % STREAM_END_MARK,
                )
            ],
            mark=STREAM_END_MARK,
        )
    print("and to keep a stream alive when a piece read fails:")
    before = FSTREAM.read_text() if FSTREAM.exists() else ""
    if FSTREAM_ERROR_OLD in before and STREAM_RETRY_MARK not in before:
        print("and to retry that read on a timer too:")
        patch(FSTREAM, [(FSTREAM_ERROR_OLD, FSTREAM_ERROR_NEW)], mark=STREAM_RETRY_MARK)
    elif WAKE_MARK in before:
        patch(FSTREAM, [(FSTREAM_ERROR, FSTREAM_ERROR_NEW)], mark=STREAM_MARK)
    print("and to remember only complete torrents:")
    try:
        cache_text = TSTREAM.read_text()
    except OSError:
        cache_text = ""
    if CACHE_MARK in cache_text and "version: 2" not in cache_text:
        patch(
            TSTREAM,
            [
                (
                    "    var bfVerifyCacheSave = function () {\n      try {",
                    "    // %s -- a partial verification records which pieces are\n"
                    "    // absent, and that answer goes stale when the swarm fills the\n"
                    "    // hole: the next play then refuses to serve bytes it has.\n"
                    "    var bfVerifyCacheSave = function () {\n"
                    "      try {\n"
                    "        for (var c = 0; c < torrent.pieces.length; c++) {\n"
                    "          if (!engine.bitfield.get(c)) return\n"
                    "        }" % CACHE_V2_MARK,
                ),
                (
                    "        fs.writeFileSync(bfVerifyCachePath(), JSON.stringify({\n          infoHash: infoHash,",
                    "        fs.writeFileSync(bfVerifyCachePath(), JSON.stringify({\n          version: 2,\n          infoHash: infoHash,",
                ),
                (
                    "      if (!cached || cached.infoHash !== infoHash) return false",
                    "      if (!cached || cached.version !== 2) return false\n"
                    "      if (cached.infoHash !== infoHash) return false",
                ),
            ],
            mark=CACHE_V2_MARK,
        )
    print("and to look at a piece before waiting for the swarm:")
    try:
        tuned = TSTREAM.read_text()
    except OSError:
        tuned = ""
    try:
        fstream_now = FSTREAM.read_text()
    except OSError:
        fstream_now = ""
    if CACHE_V2_MARK in tuned and LOOK_MARK not in fstream_now:
        patch(
            TSTREAM,
            [
                (
                    "        for (var c = 0; c < torrent.pieces.length; c++) {\n"
                    "          if (!engine.bitfield.get(c)) return\n"
                    "        }\n",
                    "        // Partial torrents may be remembered too: absent entries are\n"
                    "        // re-checked one at a time when a read needs them.\n",
                )
            ],
            mark=LOOK_MARK + "-cache",
        )
        patch(
            FSTREAM,
            [
                (
                    "    this._promote()\n"
                    "    return this._engine.critical(this._piece, this._critical)",
                    "    this._promote()\n"
                    "    // %s -- before waiting for anyone, look at the piece itself.\n"
                    "    this._look()\n"
                    "    return this._engine.critical(this._piece, this._critical)" % LOOK_MARK,
                ),
                (
                    "  this._promote = function () {",
                    "  // %s -- one piece at a time, at most every two seconds.\n"
                    "  this._look = function () {\n"
                    "    // Never throw from here: it runs on a read the player waits for.\n"
                    "    var self = this\n"
                    "    var index = this._piece\n"
                    "    var now = Date.now()\n"
                    "    this._looked = this._looked || {}\n"
                    "    if (this._looked[index] && now - this._looked[index] < 2000) return\n"
                    "    this._looked[index] = now\n"
                    "    this._engine.store.get(index, function (err, buffer) {\n"
                    "      if (err || !buffer) {\n"
                    "        self._engine.critical(index, self._critical)\n"
                    "        return\n"
                    "      }\n"
                    "      var hash = require('crypto').createHash('sha1').update(buffer).digest('hex')\n"
                    "      var want = self._engine.torrent && self._engine.torrent.pieces[index]\n"
                    "      if (!want || hash !== Buffer.from(want).toString('hex')) {\n"
                    "        self._engine.critical(index, self._critical)\n"
                    "        return\n"
                    "      }\n"
                    "      if (typeof console !== 'undefined') console.log('bf-stream: piece ' + index + ' was on disk after all')\n"
                    "      self._engine.bitfield.set(index, true)\n"
                    "      self.notify()\n"
                    "    })\n"
                    "  }\n\n"
                    "  this._promote = function () {" % LOOK_MARK,
                ),
            ],
            mark=LOOK_MARK,
        )
    print("and to keep that check from throwing:")
    try:
        look_now = FSTREAM.read_text()
    except OSError:
        look_now = ""
    if LOOK_OLD_BODY in look_now:
        patch(FSTREAM, [(LOOK_OLD_BODY, LOOK_SAFE_BODY)], mark=LOOK_FIX_MARK)
    print("and to keep the cache when the file grows:")
    try:
        grown = TSTREAM.read_text()
    except OSError:
        grown = ""
    # Trees installed before this rule compare sizes for equality.
    try:
        if "cached.files[i].size) return false" in grown and "actual < cached.files[i].size" not in grown:
            patch(
                TSTREAM,
                [
                    (
                        "        if (actual !== cached.files[i].size) return false",
                        "        // Growth is expected and must not throw the cache away: a play that\n"
                        "        // starts while the file is still being written sees it grow, rejected\n"
                        "        // the cache, re-verified 1.8GB -- and that verification then wedged at\n"
                        "        // its first piece for an hour, which is a bug of its own. Pieces the\n"
                        "        // file has gained are simply absent from the bitfield, and the\n"
                        "        // per-piece check in file-stream vouches for them when a read needs\n"
                        "        // them. A file that shrank, or has gone, is a different matter: the\n"
                        "        // pieces recorded as present may no longer be.\n"
                        "        if (actual === -1 ? cached.files[i].size !== -1\n"
                        "                          : actual < cached.files[i].size) return false",
                    )
                ],
                mark=CACHE_GROWTH_MARK,
            )
    except Exception:
        pass
    print("and to put a waiting read at the front of the queue:")
    # Anchored on the wakeup patch's own text, so it applies to trees that
    # already have it (every box) and skips cleanly if it is absent.
    before = FSTREAM.read_text() if FSTREAM.exists() else ""
    if WAKE_MARK in before:
        patch(
            FSTREAM,
            [
                (
                    "  this._onverify = this.notify.bind(this)\n"
                    "  engine.on('verify', this._onverify)\n}",
                    "  this._onverify = this.notify.bind(this)\n"
                    "  engine.on('verify', this._onverify)\n"
                    "  // %s -- a read waiting on a piece takes the front of the\n"
                    "  // queue. Without this its range is just one more selection,\n"
                    "  // and the download's own order wins: measured, a 5.3GB MP4\n"
                    "  // whose index is at the end of the file left VLC reading\n"
                    "  // 666MB of a film it could not start.\n"
                    "  this._promote = function () {\n"
                    "    var selections = engine.selection\n"
                    "    for (var k = 1; k < selections.length; k++) {\n"
                    "      if (selections[k].from === this.startPiece && selections[k].to === this.endPiece) {\n"
                    "        selections.unshift(selections.splice(k, 1)[0])\n"
                    "        return\n"
                    "      }\n"
                    "    }\n"
                    "  }\n}" % PARKED_MARK,
                ),
                (FSTREAM_PARK, FSTREAM_PARK_NEW),
            ],
            mark=PARKED_MARK,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

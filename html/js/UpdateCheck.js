/*
 * Reload the page when the server has been updated underneath it.
 *
 * This app is served by the machine it is talking to, and that machine gets
 * updated by git rather than by anything the browser takes part in. Nothing in
 * the page knows when that has happened: no ETag is revalidated, the document
 * is kept because the meta tags make this a PWA, and a kept document loads the
 * JavaScript it was saved with.
 *
 * The result is a browser that is weeks behind the server and has no way to
 * find out. It is not a cache that can be cleared from here -- the user does
 * that, and then something else goes stale, and in the meantime "I changed it
 * and nothing happened" is answered by guessing. It was answered by guessing
 * for three rounds, and the guess was wrong each time.
 *
 * So the page asks. The server says what it is running; if that is not what it
 * was running when the page loaded, the page reloads and picks it up.
 *
 * Never while something is playing: reloading mid-film because someone ran
 * git pull is not a trade anybody wants. The check happens again after half a
 * minute of quiet.
 */
function UpdateCheck(playerStore) {
  var self = this

  //: How often to ask, in milliseconds.
  self.every = 30000

  //: What the server was running when this page loaded. Null until it answers,
  //: because comparing against nothing would reload the page on the first tick.
  self.loaded = null

  //: Set once a reload has been asked for, so a slow reload does not queue
  //: another one behind it.
  self.reloading = false

  self.poll = function() {
    if (self.reloading || !self.loaded) return
    Utils.rpc('server', 'version', null, function(err, build) {
      if (err || !build || build === self.loaded) return
      if (playerStore.getStatus().State) return
      self.reloading = true
      window.location.reload()
    })
  }

  self.start = function() {
    Utils.rpc('server', 'version', null, function(err, build) {
      if (err || !build) return
      self.loaded = build
    })
    window.setInterval(self.poll, self.every)
  }
}
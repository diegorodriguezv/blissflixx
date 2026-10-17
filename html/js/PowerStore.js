/*
 * Reboot and shutdown: pressing the buttons, and saying in the bottom bar what
 * is happening until the machine answers again.
 *
 * The clock is the player poll and nothing else. It already asks the server
 * every second, and the difference between that succeeding and failing is the
 * only thing on the page that knows whether the Pi is up. So this listens for
 * that transition rather than starting a timer of its own to guess.
 *
 * It subscribes to the player store directly, and is handed it, rather than
 * going through RiotControl. It went through RiotControl first and that was
 * wrong in a way nothing noticed: RiotControl.on registers the listener on the
 * stores it already holds, and every store in index.html is constructed before
 * the first addStore, so the listener was attached to nothing at all. The bar
 * appeared, and then cleared on the give-up timer thirty seconds later because
 * nothing had ever told it the machine was back.
 */
function PowerStore(playerStore) {
  riot.observable(this)
  var self = this

  // What the bar says, or null when it has nothing to say.
  self.state = null

  // The action pressed and not yet resolved, and whether the server has been
  // seen going away since it was pressed.
  var pending = null
  var sawOffline = false
  var giveUp = null

  // While the action has been asked for and the machine has not gone yet.
  var ASKING = {
    reboot: 'Rebooting...',
    shutdown: 'Shutting down...'
  }

  // Once it has gone. A reboot says nothing new here and clears itself when it
  // comes back. A shutdown does not, because there is nothing coming back --
  // this is the message it settles on and keeps.
  var GONE = {
    reboot: 'Rebooting...',
    shutdown: 'Shut down.'
  }

  // How long to wait for the machine to actually go before deciding the button
  // did nothing. Only counted until it does: a reboot that takes a minute is
  // still rebooting, and clearing the bar at some arbitrary point in the middle
  // of it would be clearing it on a timer rather than on what happened.
  var GIVE_UP_AFTER = 30000

  self.power = function(action) {
    // Shown on the press, not on the reply.
    //
    // It used to wait for the request to come back first, which made the one
    // thing the bar exists for depend on a network round trip to a machine that
    // is about to stop answering. The reply arrives, usually -- but the cases
    // where it does not are precisely the cases this bar is for: the connection
    // closing mid-response, the machine going down before the bytes are
    // written, a browser answering differently from the one it was written
    // against. Any of those and the bar stayed silent while the machine did
    // exactly what it was told.
    //
    // Nothing is claimed by showing it early. A press that turns out to have
    // failed puts the error up in place of the message, which is the honest
    // sequence: something was asked for, and then either it happened or it did
    // not.
    if (giveUp) window.clearTimeout(giveUp)
    pending = action
    sawOffline = false
    say(ASKING[action])

    Utils.rpc('server', action, null, function(err) {
      // An offline error is not the button failing: systemctl queues the job
      // and returns, so if the machine went down before the reply was written
      // the reply never arrives, and that is the button working.
      if (err && !Utils.isOffline(err)) {
        forget()
        return Utils.showError(err)
      }
      // Nothing further to do on success. The bar is already up and stays up
      // until the machine has gone and come back.
      giveUp = window.setTimeout(function() {
        // Asked for, and the machine never went. It is up and answering, so the
        // action did not take -- say so by saying nothing.
        forget()
      }, GIVE_UP_AFTER)
    })
  }

  self.getState = function() {
    return self.state
  }

  playerStore.on('se_online_changed', function(online) {
    if (!pending) return
    if (!online) {
      sawOffline = true
      // It has gone. Nothing more to wait for but its return, so stop counting
      // the time it was supposed to take.
      if (giveUp) {
        window.clearTimeout(giveUp)
        giveUp = null
      }
      if (pending === 'shutdown') say(GONE.shutdown)
      return
    }
    // Back, having been gone: a finished reboot.
    if (sawOffline) forget()
  })

  function say(message) {
    self.state = message
    self.trigger('se_power_changed')
  }

  function forget() {
    if (giveUp) {
      window.clearTimeout(giveUp)
      giveUp = null
    }
    pending = null
    sawOffline = false
    self.state = null
    self.trigger('se_power_changed')
  }
}
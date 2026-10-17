/*
 * Reboot and shutdown: pressing the buttons, and saying in the bottom bar what
 * is happening until the machine answers again.
 *
 * The clock is the player poll and nothing else. It already asks the server
 * every second, and the difference between that succeeding and failing is the
 * only thing on the page that knows whether the Pi is up. So this listens for
 * that transition rather than starting a timer of its own to guess.
 */
function PowerStore() {
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
    Utils.rpc('server', action, null, function(err) {
      // An offline error here is not the button failing. systemctl queues the
      // job and returns; if the machine went down before the reply was written,
      // the reply never arrives, and that is the button working.
      if (err && !Utils.isOffline(err)) return Utils.showError(err)
      // Any timer from a previous press is cancelled rather than left running.
      // Two presses leave two timers, the first one still armed, and when it
      // fires it clears the message the second press just put up -- in the
      // middle of the reboot, and for no reason anyone can see.
      if (giveUp) window.clearTimeout(giveUp)
      pending = action
      sawOffline = false
      say(ASKING[action])
      giveUp = window.setTimeout(function() {
        // Asked for, and the machine never went. It is up and answering, so
        // the action did not take -- say so by saying nothing.
        forget()
      }, GIVE_UP_AFTER)
    })
  }

  self.getState = function() {
    return self.state
  }

  RiotControl.on('se_online_changed', function(online) {
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
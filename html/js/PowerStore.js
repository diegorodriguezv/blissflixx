/*
 * Reboot and shutdown: one line of text in the bar the player already uses for
 * errors, saying what the machine is doing to itself.
 *
 * It goes away by itself, and that part needs nothing written for it. The page
 * asks the server for status every second. It stops being able to while the
 * machine is down and starts again when it is back, so the first answer after a
 * silence is the machine being back. The same poll is what already decides when
 * an error message stops being shown, which is why this is a message in that
 * bar rather than a new one.
 *
 * A shutdown has no return, so its message stays up. That is the honest thing
 * to leave on screen: the machine is off and nobody is coming to press a key.
 */
function PowerStore(playerStore) {
  riot.observable(this)
  var self = this

  // The text, or null when there is nothing to say.
  self.msg = null

  // Whether the machine has been seen unreachable since this was pressed.
  var wentAway = false

  self.power = function(action) {
    // Said on the press, not on the reply. The reply comes from a machine that
    // is about to stop answering, so not getting one is the ordinary case
    // rather than the exceptional one, and waiting for it leaves the screen
    // saying nothing through exactly the moment it exists to speak.
    wentAway = false
    self.msg = action === 'shutdown' ? 'Shutting down...' : 'Rebooting...'
    self.trigger('se_power_changed')

    Utils.rpc('server', action, null, function(err) {
      // No reply at all, or an unreachable one, is the button working: the
      // machine went down before it could answer. Anything else is a real
      // refusal and replaces this with the error.
      if (!err || Utils.isOffline(err)) return
      self.msg = null
      self.trigger('se_power_changed')
      Utils.showError(err)
    })
  }

  self.getMsg = function() {
    return self.msg
  }

  playerStore.on('se_online_changed', function(online) {
    if (!online) wentAway = true
  })

  playerStore.on('se_status_changed', function() {
    // Answering again, having not answered a moment ago: the machine is back.
    if (!self.msg || !wentAway) return
    self.msg = null
    wentAway = false
    self.trigger('se_power_changed')
  })
}
/*
 * Reboot and shutdown: one line of text in the bar the player already uses for
 * errors, saying what the machine is doing to itself.
 *
 * The server says what it is doing in the answer it already uses for errors --
 * the status every tab asks for once a second -- so the machine being back is
 * the first status that does not say it is going down, and this tab takes the
 * message down then. One rule, and it is the server's answer rather than
 * anything this tab worked out for itself: a tab cannot tell whether the
 * machine is coming back, because coming back and being asked about it look the
 * same from here. The server can.
 *
 * That used to be the other way round. The message came down only once this tab
 * had seen the machine go away and answer again, and on a real reboot it does
 * not: the network goes down while there are requests in flight, and a
 * connection to a machine that has stopped answering hangs rather than being
 * refused. So this tab was never told. It said "Rebooting..." for as long as it
 * was left open, on a machine that was plainly back -- while the tab beside it,
 * which had only ever had the server's answer, was already showing nothing.
 *
 * The text here is still put up on the press rather than waiting for that first
 * status, which covers the second between the button and the answer, and the
 * case where the server cannot be asked at all.
 *
 * While the machine is down there is no status to contradict it, so the message
 * stays. For a shutdown that is the end of it and is meant: the machine is off
 * and nobody is coming to press a key.
 */
function PowerStore(playerStore) {
  riot.observable(this)
  var self = this

  // The text, or null when there is nothing to say.
  self.msg = null

  self.power = function(action) {
    // Said on the press, not on the reply. The reply comes from a machine that
    // is about to stop answering, so not getting one is the ordinary case
    // rather than the exceptional one, and waiting for it leaves the screen
    // saying nothing through exactly the moment it exists to speak.
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

  playerStore.on('se_status_changed', function() {
    if (!self.msg) return
    // Still going down, so still nothing to say.
    if (playerStore.getStatus().Restart) return
    // The machine is answering and says it is not going down. Whatever this tab
    // was told by its own click, the machine has the last word.
    self.msg = null
    self.trigger('se_power_changed')
  })
}
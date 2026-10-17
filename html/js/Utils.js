var Utils = {
  rpc: function(module, fn, data, cb) {
    var url = '/api/' +  module
    payload = { fn: fn }
    if (data) payload['data'] = JSON.stringify(data)
    $.getJSON(url, payload, function(data) {
      cb(null, data)
    }).fail(function(xhr, textStatus) {
      var msg = ''
      if (xhr.status === 0) {
        return cb(Utils.createError('offline','offline'))
      }
      else if (xhr.responseText.lastIndexOf('{', 0) === 0) {
        obj = JSON.parse(xhr.responseText)
        msg = obj.error
      } else {
        msg = xhr.responseText
      }
      cb(Utils.createError(textStatus + ": " + xhr.status, msg))
    })
  },
isOffline: function(err) {
    /*
     * Whether this is the "could not reach the server" error rather than the
     * server saying no.
     *
     * rpc() builds two different things and neither says which it is. A
     * rejected request with a body is the server's answer, and it must be shown.
     * A failed connection has no body and is reported as '/offline/offline',
     * which is not a fault in the server and is not something the person
     * pressing the button can do anything about.
     *
     * The one place that matters is reboot and shutdown: the machine is asked
     * to go down and then the reply never arrives, because the machine went
     * down. Treating that as a failure would put an error page in front of
     * someone whose button worked.
     */
    return typeof err === 'string' && err.indexOf('/offline/') === 0
  },
  goRoute: function() {
    var route = arguments[0]
    for (i=1; i<arguments.length; i++) {
      route += '/' + encodeURIComponent(arguments[i])
    }
    riot.route(route)
  },
  createError: function(type, msg) {
    return '/' + encodeURIComponent(type) + '/' + encodeURIComponent(msg)
  },
  showError: function(err) {
    // While the machine is rebooting or shutting down, the server being
    // unreachable is the state that was asked for, not a fault in it.
    //
    // Something polls the server every second and something routes here when a
    // poll fails, so the moment the machine goes down -- which for a reboot is
    // a second after the button -- the whole page is replaced by:
    //
    //   It looks like the server is not running. Please restart the server
    //   and try again.
    //
    // On the screen of someone who just asked it to reboot, telling them to
    // restart the thing they are already restarting, and burying the bar that
    // says what is actually happening underneath a page about a fault that
    // does not exist.
    //
    // So this says nothing while a power action is in flight. The bar at the
    // bottom already covers it, and the error is still there to come back to
    // for any request that genuinely failed.
    //
    // Either way of knowing that, because this tab may not be the one that
    // pressed the button. The machine is announced in every status poll, so a
    // tab that only watched is told as well -- and without that half, a click
    // here during a reboot asked for in the tab beside it replaced the page
    // with "please restart the server" while the server was rebooting because
    // somebody had asked it to.
    //
    // Read from the server's own answer, because the server is the only thing
    // that knows about a press made anywhere but this tab, and because it is
    // also what knows the difference between a message that is a fault and one
    // that is the machine leaving.
    if (typeof powerStore !== 'undefined' && powerStore.getMsg()) return
    if (typeof playerStore !== 'undefined' && playerStore.getStatus().Restart) return
    riot.route('error' + err)
  },
  decodeURI: function(val) {
    val = decodeURIComponent(val)
    if (val === 'undefined') val = ''
    return val
  },
}

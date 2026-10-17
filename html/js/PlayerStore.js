function PlayerStore() {
  riot.observable(this)
  var self = this
  self.status = {}

  // Whether the last poll reached the server. The poll has always thrown its
  // failures away on the grounds that the server is probably not running, and
  // that is still true and still thrown away. But down-to-up is the only thing
  // on the page that knows a reboot has finished, so the transition is published
  // rather than the failure that used to be the only sign of it.
  self.online = true

  window.setInterval(function(){
    Utils.rpc('playr', 'status', null, function(err, data) {
      if (err) {
        if (self.online) {
          self.online = false
          self.trigger('se_online_changed', false)
        }
        return /* Ignore errors - probably server not running */
      }
      if (!self.online) {
        self.online = true
        self.trigger('se_online_changed', true)
      }
      self.status = data
      self.trigger('se_status_changed')
    })
  }, 1000);

  self.play = function(url, title, cb) {
    Utils.rpc('playr', 'play', {url: url, title: title}, cb)
  }

  self.playWithSubs = function(url, title, subs, cb) {
    Utils.rpc('playr', 'play', {url: url, title: title, subs:subs}, cb)
  }

  self.control = function(action, cb) {
    Utils.rpc('playr', 'control', {action: action}, cb)
  }

  self.getStatus = function() {
    return self.status
  }
}
function PlayerStore() {
  riot.observable(this)
  var self = this
  self.status = {}

  // Whether the last poll reached the server. The poll has always thrown its
  // failures away on the grounds that the server is probably not running, and
  // that is still true and still thrown away.
  //
  // The transition is published because it is real state worth having -- but it
  // used to have a second reason, which has gone: down-to-up was the only thing
  // on the page that knew a reboot had finished, and the reboot bar hung off it.
  // It does not now. During a real reboot the network drops while there are
  // requests in flight, and a connection to a machine that has stopped
  // answering hangs rather than being refused, so this transition often simply
  // never happens -- see PowerStore, which takes the bar down from the server's
  // own answer instead.
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

  // plid and itemnum are the playlist a row came from, when it came from one.
  // They are what lets the server keep going with the next item once this film
  // ends, or when the playbar's item buttons are pressed. A row from a channel
  // or a search has neither, and plays on its own as it always did.
  self.play = function(url, title, cb, plid, itemnum) {
    var args = {url: url, title: title}
    if (plid !== undefined && itemnum !== undefined) {
      args.plid = plid
      args.itemnum = itemnum
    }
    Utils.rpc('playr', 'play', args, cb)
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
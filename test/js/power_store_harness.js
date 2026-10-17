// Drives PowerStore through the sequences that matter. Run by
// test/test_power_store.py, skipped there when node is not installed.

const fs = require('fs');
const path = require('path');

// The Python side of this is test/test_power_store.py, which skips when node
// is not installed. There is no JavaScript test runner in this project, so this
// is a plain node script that exits non-zero and prints what failed.
const REPO = path.resolve(__dirname, '..', '..');
const load = (p) => fs.readFileSync(path.join(REPO, p), 'utf8');

const failures = [];
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) failures.push(name);
  console.log(`  ${ok ? 'ok  ' : 'FAIL'}  ${name}`);
  if (!ok) {
    console.log(`          expected: ${JSON.stringify(expected)}`);
    console.log(`          actual:   ${JSON.stringify(actual)}`);
  }
}

// ---------------------------------------------------------------- harness ---

function makeWorld() {
  const listeners = {};
  const world = {
    timers: new Map(),
    nextTimer: 1,
    now: 0,
    errors: [],
    shown: [],
    calls: [],
  };

  // Modelled the way the real RiotControl behaves, which is the point of this
  // rewrite. The real one walks the stores it already holds:
  //
  //     RiotControl.on = function () {
  //       this._stores.forEach(el => el.on(...args))
  //     }
  //
  // The first version of this harness recorded every listener unconditionally
  // and so passed a store whose RiotControl.on ran inside its constructor --
  // which in index.html is before the first addStore, so the real one had
  // attached it to nothing. The stub hid the exact bug it was written to catch,
  // and every sequence below still passed with the listener never delivered.
  world.stores = [];
  world.RiotControl = {
    on(event, fn) {
      // Attached to nothing if there are no stores yet. That is the whole
      // behaviour, and it is silent in the real code too.
      world.stores.forEach((s) => s.on(event, fn));
    },
    off(event, fn) {
      world.stores.forEach((s) => s.off(event, fn));
    },
    trigger(event, arg) {
      world.stores.forEach((s) => s.trigger(event, arg));
    },
    addStore(store) {
      world.stores.push(store);
    },
    listeners,
  };

  world.riot = {
    observable(self) {
      self._listeners = {};
      self.on = (e, fn) => (self._listeners[e] = self._listeners[e] || []).push(fn);
      self.off = (e) => delete self._listeners[e];
      self.trigger = (e, a) => (self._listeners[e] || []).forEach((fn) => fn(a));
      return self;
    },
  };

  world.Utils = {
    rpc(module, fn, data, cb) {
      world.calls.push(`${module}.${fn}`);
      world.respond(cb);
    },
    isOffline: (err) => typeof err === 'string' && err.indexOf('/offline/') === 0,
    showError(err) {
      world.shown.push(err);
    },
  };

  world.window = {
    setTimeout(fn) {
      const id = world.nextTimer++;
      world.timers.set(id, fn);
      return id;
    },
    clearTimeout(id) {
      world.timers.delete(id);
    },
    // PlayerStore starts a one-second poll. Never fires it -- the sequences
    // drive the transition explicitly, through world.poll -- but the call has
    // to exist or constructing it throws.
    setInterval() {
      return 0;
    },
    clearInterval() {},
  };

  world.runTimers = () => {
    const pending = [...world.timers.values()];
    world.timers.clear();
    pending.forEach((fn) => fn());
  };
  world.timerCount = () => world.timers.size;

  // Load PowerStore against this world.
  const src = load('html/js/PowerStore.js');
  const PowerStore = new Function(
    'riot', 'RiotControl', 'Utils', 'window',
    src + '\n;return PowerStore;'
  )(world.riot, world.RiotControl, world.Utils, world.window);
  world.PowerStore = PowerStore;

  // index.html's order, deliberately: every store is constructed before the
  // first addStore.
  const PlayerStore = new Function(
    'riot', 'RiotControl', 'Utils', 'window',
    load('html/js/PlayerStore.js') + '\n;return PlayerStore;'
  )(world.riot, world.RiotControl, world.Utils, world.window);
  world.playerStore = new PlayerStore();
  world.store = new PowerStore(world.playerStore);
  world.RiotControl.addStore(world.playerStore);
  world.RiotControl.addStore(world.store);

  // Drive the real publisher rather than a bare function, so "does anyone
  // hear this" is the thing under test.
  world.poll = (err) => {
    if (err) {
      if (world.playerStore.online) {
        world.playerStore.online = false;
        world.playerStore.trigger('se_online_changed', false);
      }
      return;
    }
    if (!world.playerStore.online) {
      world.playerStore.online = true;
      world.playerStore.trigger('se_online_changed', true);
    }
  };

  return world;
}

// ------------------------------------------------------------------ cases ---

console.log('PowerStore');

console.log('\n  the button was pressed and nothing has happened yet');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  check('message appears at once', w.store.getState(), 'Rebooting...');
  check('no error shown', w.shown, []);
  check('server asked', w.calls, ['server.reboot']);
  check('a give-up timer is running', w.timerCount(), 1);
}

console.log('\n  the server comes straight back without ever going away');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  // The very first poll after systemctl returns still succeeds, because the
  // machine is up for another second or two. This must not clear the bar.
  w.poll(null);
  check('message stays up', w.store.getState(), 'Rebooting...');
}

console.log('\n  the machine goes down and comes back: a finished reboot');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  w.poll('offline');
  check('still says rebooting while it is down', w.store.getState(), 'Rebooting...');
  check('give-up timer cancelled, it has gone', w.timerCount(), 0);
  w.poll(null);
  check('message clears when it is back', w.store.getState(), null);
  check('no error shown', w.shown, []);
}

console.log('\n  a reboot that takes longer than the give-up timer');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  w.poll('offline');
  // Ten minutes later, still down.
  w.now += 600000;
  check('still saying rebooting', w.store.getState(), 'Rebooting...');
  w.poll(null);
  check('and it clears on return, whenever that is', w.store.getState(), null);
}

console.log('\n  the button did nothing at all');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  w.runTimers();
  check('message clears rather than sticking', w.store.getState(), null);
  check('timer is not left running', w.timerCount(), 0);
}

console.log('\n  shutdown: the machine does not come back');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('shutdown');
  check('message appears', w.store.getState(), 'Shutting down...');
  w.poll('offline');
  check('settles on shut down', w.store.getState(), 'Shut down.');
  // If the machine is somehow up again it did not shut down, so the message
  // that says it did has to go -- which is also what was asked for: the bar
  // clears when the system is back online.
  w.poll(null);
  check('and clears if it turns out to be back', w.store.getState(), null);
}

console.log('\n  the reply never arrives, because the machine went down first');
{
  const w = makeWorld();
  w.respond = (cb) => cb(UtilsOffline());
  function UtilsOffline() { return '/offline/offline'; }
  w.store.power('reboot');
  check('treated as the button working', w.store.getState(), 'Rebooting...');
  check('no error page', w.shown, []);
}

console.log('\n  the server said no');
{
  const w = makeWorld();
  w.respond = (cb) => cb('/parsererror%3A%20500/Could not reboot: ...');
  w.store.power('reboot');
  check('error is shown, not swallowed', w.shown.length, 1);
  check('and no bar', w.store.getState(), null);
}

console.log('\n  a second press while one is pending');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  w.store.power('shutdown');
  w.poll('offline');
  check('the later press is the one that counts', w.store.getState(), 'Shut down.');
  check('only one timer running', w.timerCount(), 0);
}

console.log('');
if (failures.length) {
  console.log(`  ${failures.length} FAILED: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('  all passed');
// ---------------------------------------------------------------------------
// The message is shown on the press, not on the reply.
//
// These came after two reports of "no message" from someone pressing the button
// on a television and watching the machine reboot. Everything above passed
// while it was happening, because every one of those sequences answers the rpc
// immediately. The gap was the window between the press and the reply, which is
// exactly the window a machine that is about to go down makes longer.
// ---------------------------------------------------------------------------

function makeDeferredWorld() {
  const w = makeWorld();
  let release = null;
  w.held = null;
  w.respond = (cb) => { w.held = cb; };   // the request is in flight
  w.release = () => { const cb = w.held; w.held = null; cb(null, null); };
  return w;
}

console.log('\n  the request has not come back yet');
{
  const w = makeDeferredWorld();
  w.store.power('reboot');
  check('the bar is up before the reply', w.store.getState(), 'Rebooting...');
  check('and nothing has been asked to forget it', w.timerCount(), 0);
  w.release();
  check('the reply changes nothing', w.store.getState(), 'Rebooting...');
  check('and only now starts the give-up clock', w.timerCount(), 1);
}

console.log('\n  the machine goes and comes back while the reply is still in flight');
{
  const w = makeDeferredWorld();
  w.store.power('reboot');
  w.poll('offline');
  w.release();
  w.poll(null);
  check('a finished reboot clears it', w.store.getState(), null);
}

console.log('\n  the reply comes back saying no');
{
  const w = makeDeferredWorld();
  w.store.power('reboot');
  check('the bar went up on the press', w.store.getState(), 'Rebooting...');
  w.held('/parsererror%3A%20500/Could not reboot: ...');
  check('and is taken down again by the failure', w.store.getState(), null);
  check('with the error shown', w.shown.length, 1);
  check('and no timer left behind', w.timerCount(), 0);
}

console.log('\n  the reply never arrives because the machine went down');
{
  const w = makeDeferredWorld();
  w.store.power('shutdown');
  check('the bar went up on the press', w.store.getState(), 'Shutting down...');
  w.held('/offline/offline');
  w.poll('offline');
  check('settles on shut down', w.store.getState(), 'Shut down.');
  check('no error shown', w.shown, []);
}

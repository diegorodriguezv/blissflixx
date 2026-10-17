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

  world.RiotControl = {
    on(event, fn) {
      (listeners[event] = listeners[event] || []).push(fn);
    },
    off() {},
    trigger(event, arg) {
      (listeners[event] || []).forEach((fn) => fn(arg));
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
  world.store = new PowerStore();

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
  w.RiotControl.trigger('se_online_changed', true);
  check('message stays up', w.store.getState(), 'Rebooting...');
}

console.log('\n  the machine goes down and comes back: a finished reboot');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  w.RiotControl.trigger('se_online_changed', false);
  check('still says rebooting while it is down', w.store.getState(), 'Rebooting...');
  check('give-up timer cancelled, it has gone', w.timerCount(), 0);
  w.RiotControl.trigger('se_online_changed', true);
  check('message clears when it is back', w.store.getState(), null);
  check('no error shown', w.shown, []);
}

console.log('\n  a reboot that takes longer than the give-up timer');
{
  const w = makeWorld();
  w.respond = (cb) => cb(null, null);
  w.store.power('reboot');
  w.RiotControl.trigger('se_online_changed', false);
  // Ten minutes later, still down.
  w.now += 600000;
  check('still saying rebooting', w.store.getState(), 'Rebooting...');
  w.RiotControl.trigger('se_online_changed', true);
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
  w.RiotControl.trigger('se_online_changed', false);
  check('settles on shut down', w.store.getState(), 'Shut down.');
  // If the machine is somehow up again it did not shut down, so the message
  // that says it did has to go -- which is also what was asked for: the bar
  // clears when the system is back online.
  w.RiotControl.trigger('se_online_changed', true);
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
  w.RiotControl.trigger('se_online_changed', false);
  check('the later press is the one that counts', w.store.getState(), 'Shut down.');
  check('only one timer running', w.timerCount(), 0);
}

console.log('');
if (failures.length) {
  console.log(`  ${failures.length} FAILED: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('  all passed');
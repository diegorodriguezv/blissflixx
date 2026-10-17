// Drives PowerStore through the sequences that matter. Run by
// test/test_power_store.py, skipped there when node is not installed.
//
// The store is now small on purpose. It says one line of text, and clears it
// when the page's own status poll starts answering again -- the same poll that
// decides when an error message stops being shown, which is why the text goes
// in that bar.

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

function makeWorld() {
  const world = {
    timers: new Map(),
    nextTimer: 1,
    errors: [],
    shown: [],
    calls: [],
  };

  // Modelled the way the real RiotControl behaves: on() walks the stores it
  // already holds. The first version of this harness recorded every listener
  // unconditionally and so passed a store that subscribed in its constructor
  // -- which, in index.html, is before the first addStore, so the real
  // RiotControl had attached the listener to nothing. The stub was kinder than
  // the thing it stood in for and hid the bug it was written to catch.
  world.stores = [];
  world.RiotControl = {
    on(event, fn) { world.stores.forEach((s) => s.on(event, fn)); },
    off(event, fn) { world.stores.forEach((s) => s.off(event, fn)); },
    trigger(event, arg) { world.stores.forEach((s) => s.trigger(event, arg)); },
    addStore(store) { world.stores.push(store); },
  };

  world.riot = {
    observable(self) {
      self._listeners = [];
      self.on = (e, fn) => { if (e !== '*') self._listeners.push([e, fn]); };
      self.off = (e, fn) => {
        self._listeners = self._listeners.filter(([n, f]) => n !== e || f !== fn);
      };
      self.trigger = (e, a) => self._listeners
        .filter(([n]) => n === e || n === '*')
        .forEach(([, fn]) => fn(a));
      return self;
    },
  };

  world.Utils = {
    rpc(module, fn, data, cb) {
      world.calls.push(`${module}.${fn}`);
      world.respond(cb);
    },
    isOffline: (err) => typeof err === 'string' && err.indexOf('/offline/') === 0,
    showError(err) { world.shown.push(err); },
  };

  world.window = {
    setTimeout(fn) { const id = world.nextTimer++; world.timers.set(id, fn); return id; },
    clearTimeout(id) { world.timers.delete(id); },
    // PlayerStore starts a one-second poll. Never fired here -- the sequences
    // drive it through world.poll -- but the call has to exist.
    setInterval() { return 0; },
    clearInterval() {},
  };

  world.timerCount = () => world.timers.size;
  world.runTimers = () => {
    const pending = [...world.timers.values()];
    world.timers.clear();
    pending.forEach((fn) => fn());
  };

  const compile = (file, name) => new Function(
    'riot', 'RiotControl', 'Utils', 'window',
    load(file) + `\n;return ${name};`
  )(world.riot, world.RiotControl, world.Utils, world.window);

  const PlayerStore = compile('html/js/PlayerStore.js', 'PlayerStore');
  const PowerStore = compile('html/js/PowerStore.js', 'PowerStore');

  // index.html's order: every store is built before the first addStore.
  world.playerStore = new PlayerStore();
  world.store = new PowerStore(world.playerStore);
  world.RiotControl.addStore(world.playerStore);
  world.RiotControl.addStore(world.store);

  // One second of the page's status poll.
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
    world.playerStore.status = { State: 0, Msg: '', Title: '', Paused: false, Error: false };
    world.playerStore.trigger('se_status_changed');
  };

  world.respond = (cb) => cb(null, null);
  return world;
}

console.log('PowerStore');

console.log('\n  the button was pressed');
{
  const w = makeWorld();
  w.store.power('reboot');
  check('says it is rebooting', w.store.getMsg(), 'Rebooting...');
  check('no error shown', w.shown, []);
  check('the server was asked', w.calls, ['server.reboot']);
}

console.log('\n  shutdown says something different');
{
  const w = makeWorld();
  w.store.power('shutdown');
  check('says it is shutting down', w.store.getMsg(), 'Shutting down...');
}

console.log('\n  the page is still answering, so nothing has happened yet');
{
  const w = makeWorld();
  w.store.power('reboot');
  w.poll(null);          // a normal successful status poll, machine still up
  w.poll(null);
  check('the message stays', w.store.getMsg(), 'Rebooting...');
}

console.log('\n  the machine goes down and comes back');
{
  const w = makeWorld();
  w.store.power('reboot');
  w.poll('offline');
  check('still saying, while it is down', w.store.getMsg(), 'Rebooting...');
  w.poll(null);
  check('gone, the moment it answers again', w.store.getMsg(), null);
}

console.log('\n  a reboot that takes longer than a second or two');
{
  const w = makeWorld();
  w.store.power('reboot');
  w.poll('offline');
  for (let i = 0; i < 90; i++) w.poll('offline');   // a minute and a half
  check('still saying', w.store.getMsg(), 'Rebooting...');
  w.poll(null);
  check('and gone on the first answer back', w.store.getMsg(), null);
}

console.log('\n  shutdown: the machine does not come back');
{
  const w = makeWorld();
  w.store.power('shutdown');
  w.poll('offline');
  check('says it has shut down', w.store.getMsg(), 'Shutting down...');
  w.poll('offline');
  w.poll('offline');
  check('and stays, because nothing came back', w.store.getMsg(), 'Shutting down...');
}

console.log('\n  the reply never arrives, because the machine went down first');
{
  const w = makeWorld();
  w.respond = (cb) => cb('/offline/offline');
  w.store.power('reboot');
  check('treated as the button working', w.store.getMsg(), 'Rebooting...');
  check('no error page', w.shown, []);
}

console.log('\n  the server said no');
{
  const w = makeWorld();
  w.respond = (cb) => cb('/parsererror%3A%20500/Could not reboot: ...');
  w.store.power('reboot');
  check('the message is taken down', w.store.getMsg(), null);
  check('and the error is shown', w.shown.length, 1);
}

console.log('\n  no timers at all');
{
  const w = makeWorld();
  w.store.power('reboot');
  w.poll('offline');
  w.poll(null);
  check('nothing was left armed', w.timerCount(), 0);
  w.store.power('shutdown');
  w.runTimers();
  check('and pressing again leaves nothing either', w.timerCount(), 0);
  check('the new message is up regardless', w.store.getMsg(), 'Shutting down...');
}

console.log('\n  a second press while one is pending');
{
  const w = makeWorld();
  w.store.power('reboot');
  w.store.power('shutdown');
  check('the later press is the one shown', w.store.getMsg(), 'Shutting down...');
  w.poll('offline');
  w.poll(null);
  check('and it clears on the way back', w.store.getMsg(), null);
}

console.log('');
if (failures.length) {
  console.log(`  ${failures.length} FAILED: ${failures.join(', ')}`);
  process.exit(1);
}
console.log('  all passed');
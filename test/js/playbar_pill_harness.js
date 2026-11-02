// Drive the playbar's pill with made-up status polls.
//
// The pill is the only way into the diagnostics panel, so it must not be
// missing while something is playing. It was: VLC's http interface reports no
// position now and then -- for a live stream most of the time -- and the bar
// cleared its clock on every such poll. On a live stream, once the loading
// count's five seconds were up, the pill was gone for good.
//
// The pill is also where a live stream says it is running. The player's
// position for one is the rolling HLS window (it climbs past the length it
// reports and never comes back), so the bar shows the time since the picture started
// instead, ticking on its own rather than whenever a poll happens to arrive.
//
// This compiles the real tag with the same riot and DOM shim as
// riot_tags_check.js, then calls updateStatus with status objects shaped like
// the server's. No browser: the tag only needs a clock, timers, and
// playerStore.getStatus().

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  navigator: { userAgent: 'node' },
  top: {},
  location: { href: 'http://localhost/', hash: '' },
  addEventListener: function () {},
  attachEvent: function () {},
  exports: undefined,
  define: undefined,
  require: undefined,
  module: undefined,
};
sandbox.window = sandbox;
sandbox.self = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

function makeDocument() {
  const documentElement = {
    childNodes: [],
    appendChild(node) {
      if (node && typeof node._run === 'function') node._run();
      this.childNodes.push(node);
    },
    removeChild(node) {
      const i = this.childNodes.indexOf(node);
      if (i >= 0) this.childNodes.splice(i, 1);
    },
  };
  return {
    documentElement,
    createElement(tag) {
      const node = {
        tagName: String(tag).toUpperCase(),
        childNodes: [],
        setAttribute() {},
        appendChild(child) { this.childNodes.push(child); },
      };
      if (String(tag).toLowerCase() === 'script') {
        node.text = '';
        Object.defineProperty(node, 'text', {
          get() { return this._text; },
          set(value) {
            this._text = value;
            this._run = () => vm.runInContext(value, sandbox, { filename: 'riot-tag.js' });
          },
        });
      }
      return node;
    },
    createTextNode: () => ({}),
    createDocumentFragment: () => ({ childNodes: [] }),
    body: { appendChild() {}, childNodes: [] },
  };
}
sandbox.document = makeDocument();

vm.runInContext(
  fs.readFileSync(path.join(REPO, 'html/js/riot2.0.12.js'), 'utf8'),
  sandbox,
  { filename: 'riot2.0.12.js' }
);

const riot = sandbox.riot;

// Capture the tag's function instead of mounting it.
let registered;
const realTag = riot.tag;
riot.tag = function (name, html, css, fn) {
  registered = fn || css;
  return realTag.apply(this, arguments);
};
riot.compile(fs.readFileSync(path.join(REPO, 'html/tags/playbar.html'), 'utf8'));

// The tag reads the clock from Date.now() and schedules its linger with
// setTimeout. Both are the harness's to move.
let now = 0;
let pending = [];
vm.runInContext('Date.now = function () { return __now }', sandbox);
sandbox.__now = now;
sandbox.setTimeout = function (fn) {
  pending.push(fn);
  return pending.length;
};
sandbox.clearTimeout = function () {};

function tick(ms) {
  now += ms;
  sandbox.__now = now;
}

function poll(tag, status) {
  sandbox.playerStore = { getStatus: () => status };
  tag.updateStatus();
  return tag.pill;
}

function fireLinger() {
  const fn = pending.pop();
  if (!fn) throw new Error('no linger was scheduled');
  fn();
}

function status(extra) {
  return Object.assign(
    {
      State: 3,
      Title: 'A Film',
      Elapsed: 0,
      Waiting: false,
      Detail: '',
      Error: false,
      Notice: '',
      Paused: false,
      Stalled: 0,
      StallReason: '',
      Live: false,
    },
    extra
  );
}

function newTag() {
  pending = [];
  const tag = {
    on() {},
    update() {},
    hideRemote() {},
    showing: false,
  };
  registered.call(tag);
  tag.silentPolls = 0;
  tag.lastCount = '';
  tag.lingering = false;
  tag.status = {};
  return tag;
}

let failed = 0;

function check(name, actual, expected) {
  if (actual !== expected) {
    console.log(`  FAIL  ${name}`);
    console.log(`          expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    failed++;
  } else {
    console.log(`  ok    ${name}`);
  }
}

function checkNotEmpty(name, actual) {
  if (!actual) {
    console.log(`  FAIL  ${name}`);
    console.log('          the pill is hidden while a film is playing');
    failed++;
  } else {
    console.log(`  ok    ${name}  (${actual})`);
  }
}

console.log('playbar pill');
now = 0;

// The loading count, held for five seconds once the picture starts.
{
  const tag = newTag();
  check('the count while loading', poll(tag, status({ State: 1, Title: 'LOADING STREAM', Elapsed: 3 })), '3s');
  check('it follows the wait', poll(tag, status({ State: 1, Title: 'LOADING STREAM', Elapsed: 4 })), '4s');
  check('and stays when the picture starts', poll(tag, status({ Elapsed: 5 })), '4s');
  fireLinger();
  checkNotEmpty('then hands over to a clock even with no position', tag.pill);
  check('which counts from the play request', tag.pill, '0:05');
}

// A film whose player answers only some polls (the live-channel pattern):
// the last position is kept, so the clock neither vanishes nor jumps back.
{
  const tag = newTag();
  poll(tag, status({ State: 1, Title: 'LOADING STREAM', Elapsed: 3 }));
  poll(tag, status({ Elapsed: 3 }));
  fireLinger();
  check('a film with a position shows it', poll(tag, status({ Elapsed: 6, Position: 4 })), '0:04');
  tick(1000);
  checkNotEmpty('a poll with no position does not hide it', poll(tag, status({ Elapsed: 7 })));
  check('and keeps the last reading', tag.pill, '0:05');
  tick(1000);
  check('the next reading takes over', poll(tag, status({ Elapsed: 8, Position: 9 })), '0:09');
}

// A live stream: the player's number is the rolling window, so it is not a
// position at all. The clock counts from the picture and ticks on its own,
// which is what shows the thing is running.
{
  const tag = newTag();
  check('live: the count while loading',
    poll(tag, status({ State: 1, Title: 'LOADING STREAM', Elapsed: 7, Live: true })), '7s');
  check('live: held when the picture starts', poll(tag, status({ Elapsed: 8, Live: true })), '7s');
  tick(5000);
  fireLinger();
  check('live: the clock starts at the picture', tag.pill, '0:05');
  tick(1000);
  check('live: it ticks without a status poll', tag.clockText(), '0:06');
  check('live: a position does not become the clock',
    poll(tag, status({ Elapsed: 20, Position: 4, Live: true })), '0:06');
}

// Paused live: the picture is not running, so neither is its clock.
{
  const tag = newTag();
  poll(tag, status({ State: 1, Title: 'LOADING STREAM', Elapsed: 4, Live: true }));
  poll(tag, status({ Title: 'Live', Elapsed: 5, Live: true }));
  tick(5000);
  fireLinger();
  check('live: the clock runs', tag.pill, '0:05');
  check('live: pause freezes it',
    poll(tag, status({ Title: 'Live', Elapsed: 6, Live: true, Paused: true })), '0:05');
  tick(3000);
  check('live: it stays frozen', tag.clockText(), '0:05');
  check('live: resume carries on from where it stopped',
    poll(tag, status({ Title: 'Live', Elapsed: 9, Live: true })), '0:05');
  tick(1000);
  check('live: and ticks again', tag.clockText(), '0:06');
}

// A live stream already playing when the page opens: there is no picture
// start to have seen, so its own age is the closest thing.
{
  const tag = newTag();
  check('live: a stream found mid-play counts its age',
    poll(tag, status({ Title: 'Live', Elapsed: 600, Live: true })), '10:00');
  tick(1000);
  check('live: and keeps ticking', tag.clockText(), '10:01');
}

// A film: the same missed sample, without handing the pill to the count.
{
  const tag = newTag();
  check('a film shows its position', poll(tag, status({ Elapsed: 5, Position: 100 })), '1:40');
  tick(1000);
  checkNotEmpty('a missed sample keeps the clock', poll(tag, status({ Elapsed: 6 })));
  check('at the last reading plus the allowed lead', tag.pill, '1:41');
  check('the next sample takes over', poll(tag, status({ Elapsed: 7, Position: 103 })), '1:43');
}

// A new play between two polls: its first playing poll has no position, and
// it must not inherit the old film's clock.
{
  const tag = newTag();
  poll(tag, status({ Title: 'First', Elapsed: 600, Position: 600 }));
  check('the old film is where it was', tag.pill, '10:00');
  check('a new title starts the clock over', poll(tag, status({ Title: 'Second', Elapsed: 0 })), '0:00');
}

// A play fast enough to go from stopped to playing between two polls: the bar
// never saw it loading, so there is no count to linger -- but there is still a
// clock, and the pill must be there.
{
  const tag = newTag();
  poll(tag, status({ State: 0, Title: '', Elapsed: 0 }));
  checkNotEmpty('a start between polls still has a pill', poll(tag, status({ Title: 'Fast', Elapsed: 0 })));
  check('counting from the request', tag.pill, '0:00');
}

// Nothing playing: the pill is gone, which is how the bar is meant to look.
{
  const tag = newTag();
  poll(tag, status({ Title: 'A Film', Elapsed: 3 }));
  const pill = poll(tag, status({ State: 0, Title: '', Elapsed: 0 }));
  check('nothing playing hides the pill', pill, '');
}

if (failed) {
  console.log(`${failed} failed`);
  process.exit(1);
}
console.log('all pill checks passed');

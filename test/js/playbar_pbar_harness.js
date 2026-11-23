// Drive the playbar's position bar with made-up status polls.
//
// The bar is hidden until the film's name is clicked, and while it is hidden
// the interface is what it always was. It only exists for a film: a live
// stream has no length to place a click in, and a film still loading is not
// anywhere yet. The marker moves on the same interpolated number the pill
// prints, and a click on the strip is an absolute seek.
//
// This compiles the real tag with the same riot and DOM shim as
// playbar_pill_harness.js, then calls updateStatus with status objects shaped
// like the server's. No browser: the tag needs a clock, timers, and
// playerStore.

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

// The tag reads the clock from Date.now().
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

// The one store the tag asks: where the player is, and what it was told.
const store = {
  status: null,
  controls: [],
  getStatus() { return store.status; },
  control(action, cb) {
    store.controls.push(action);
    if (cb) cb(null);
  },
};
sandbox.playerStore = store;

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
    // No DOM here; the marker's place is kept on the tag for these checks.
    root: { querySelector: () => null },
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
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) {
    console.log(`  FAIL  ${name}`);
    console.log(`          expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
    failed++;
  } else {
    console.log(`  ok    ${name}`);
  }
}

function clickAt(tag, x, width) {
  tag.seekTo({
    currentTarget: { getBoundingClientRect: () => ({ left: 0, width: width }) },
    clientX: x,
  });
}

console.log('playbar pbar');
now = 0;

// A film: the bar is hidden until the name is clicked, and the name toggles.
{
  const tag = newTag();
  store.status = status({ Position: 1500, Length: 6000 });
  tag.updateStatus();
  check('hidden before the name is clicked', tag.pbar, false);
  tag.togglePbar();
  check('the name shows it', tag.pbar, true);
  tag.togglePbar();
  check('and the name hides it again', tag.pbar, false);
}

// The marker: the same interpolated number the pill prints.
{
  const tag = newTag();
  store.status = status({ Position: 1500, Length: 6000 });
  tag.updateStatus();
  tag.togglePbar();
  check('the marker sits at the film\'s place', Math.round(tag.markerPct), 25);
  tick(1000);
  tag.moveMarker();
  check('and moves with the clock', tag.markerPct > 25, true);
  store.status = status({ Position: 1500, Length: 6000, Paused: true });
  tag.updateStatus();
  tick(3000);
  tag.moveMarker();
  check('a paused film\'s marker stays put', Math.round(tag.markerPct), 25);
}

// A click is an absolute seek to the place that was clicked.
{
  const tag = newTag();
  store.status = status({ Position: 100, Length: 6000 });
  tag.updateStatus();
  tag.togglePbar();
  clickAt(tag, 250, 1000);
  check('a click seeks to its place in the film', store.controls.pop(), 'seek:1500');
  clickAt(tag, 5000, 1000);
  check('past the end is the whole film', store.controls.pop(), 'seek:6000');
  clickAt(tag, -50, 1000);
  check('before the start is the beginning', store.controls.pop(), 'seek:0');
}

// Only a film has a bar. A live stream has a length reported, but it is the
// rolling window's, not a film's -- and no place to click to.
{
  const tag = newTag();
  store.status = status({ Title: 'Live', Live: true, Position: 30, Length: 60 });
  tag.updateStatus();
  tag.togglePbar();
  check('live: the name does nothing', tag.pbar, false);
  check('live: and nothing is remembered', tag.pbarOn, false);
}

// A film VLC has not measured yet: no length, nowhere to place a click.
{
  const tag = newTag();
  store.status = status({ Title: 'No Length', Position: 100, Length: 0 });
  tag.updateStatus();
  tag.togglePbar();
  check('no length: nothing to click in', tag.pbar, false);
}

// Still loading: the film is not anywhere yet.
{
  const tag = newTag();
  store.status = status({ State: 1, Title: 'Loading', Position: 0, Length: 6000 });
  tag.updateStatus();
  tag.togglePbar();
  check('loading: nothing yet', tag.pbar, false);
}

// A new play starts hidden, whatever the last film left behind.
{
  const tag = newTag();
  store.status = status({ Title: 'First', Position: 100, Length: 6000 });
  tag.updateStatus();
  tag.togglePbar();
  check('shown for the first film', tag.pbar, true);
  store.status = status({ Title: 'Second', Position: 100, Length: 6000 });
  tag.updateStatus();
  check('a new play starts hidden', tag.pbar, false);
  check('and does not remember the last one', tag.pbarOn, false);
}

if (failed) {
  console.log(`${failed} failed`);
  process.exit(1);
}
console.log('all pbar checks passed');

// Compile the riot tags the way the page does, in node, with a DOM shim.
//
// A riot tag that will not compile fails silently in a browser: the element
// simply does not appear. So the bar for "Rebooting..." could have been broken
// in a way that looked exactly like a Pi that never rebooted.
//
// The one thing the shim has to get right is that appending a <script> element
// runs it. riot.globalEval() creates a script node, sets its text and appends
// it to the document element, and the browser executing that is the entire
// mechanism by which a tag registers itself. A shim that quietly stored the
// node instead would let this pass while nothing was ever registered.
//
// compile(src, cb) returns the compiled JavaScript but only evaluates it when
// there is no callback -- the callback is for the URL branch, where the file
// has to be fetched first. So this calls it with a string and no callback, then
// asks riot for the tag it should have registered.

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const REPO = path.resolve(__dirname, '..', '..');

const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  navigator: { userAgent: 'node' },
  // riot picks its server or browser branch with !this.top, and its routing
  // block then wants location, window and an event API. None of that is used
  // by compile(), which is all this asks of riot.
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

// Appending a script node runs it, as a browser does.
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
if (!riot || typeof riot.compile !== 'function') {
  console.log('  FAIL  riot did not load');
  process.exit(1);
}

// No powernote: the reboot and shutdown message goes in playerr, the bar the
// player already uses for errors, which is what makes it work at all.
const TAGS = [
  'app.html',
  'tags/playbar.html',
  'tags/playerr.html',
  'pages/settings.html',
];

// riot.tag(name, html, css, fn) is the setter a compiled tag calls on itself,
// and there is no getter for it. So this wraps the setter to see what each
// compile registered, which is the thing actually worth checking: a template
// that compiles but names itself wrongly would render nothing at all.
const registered = {};
const realTag = riot.tag;
riot.tag = function (name, html, css, fn) {
  registered[name] = fn || css;
  return realTag.apply(this, arguments);
};

/*
 * riot does not reject an unclosed expression. Given "{ opts.message " with no
 * closing brace it simply fails to match, and renders the text "{ opts.message"
 * on screen instead of the value. Verified: a powernote with the brace removed
 * compiles, registers, and shows nothing but the literal expression.
 *
 * So compiling is not enough. The markup outside <script> -- which is where the
 * expressions are, and which has no other braces in any of these tags -- is
 * checked for an opening brace that is never closed.
 */
function unclosedExpression(src) {
  const markup = src.replace(/<script>[\s\S]*?<\/script>/g, '');
  for (let i = 0; i < markup.length; i++) {
    if (markup[i] !== '{') continue;
    const close = markup.indexOf('}', i);
    if (close === -1) {
      return markup.slice(i, i + 30).split('\n')[0].trim();
    }
    i = close;
  }
  return null;
}

let failed = 0;
console.log('riot tags compile');
for (const tag of TAGS) {
  const src = fs.readFileSync(path.join(REPO, 'html', tag), 'utf8');
  const name = path.basename(tag, '.html');
  try {
    const unclosed = unclosedExpression(src);
    if (unclosed) {
      throw new Error(
        'unclosed expression, which riot would render as literal text: ' + unclosed
      );
    }
    riot.compile(src); // no callback: compiles, evaluates, registers
    const fn = registered[name];
    if (typeof fn !== 'function') {
      throw new Error(
        `<${name}> did not register a function` +
        (fn === undefined ? ' (registered: ' + Object.keys(registered) + ')' : '')
      );
    }
    console.log(`  ok    ${tag}  ->  <${name}>`);
  } catch (e) {
    console.log(`  FAIL  ${tag}\n          ${e.message}`);
    failed++;
  }
}

console.log('');
process.exit(failed ? 1 : 0);
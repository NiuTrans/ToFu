/* ═══════════════════════════════════════════════════════════════════
 * Shared jsdom harness bootstrap for Tofu frontend tests.
 *
 * WHY
 * ---
 * Every tests/test_frontend_*.py jsdom harness used to repeat ~25-35 lines of
 * identical boilerplate: spin up JSDOM, wire global.window/document, neuter
 * the tickers (setInterval/setTimeout/requestAnimationFrame), stub the common
 * window globals (escapeHtml / t / renderMarkdown / Icon / …), and define the
 * check()/report() PASS/FAIL reporter. This module centralises that so a new
 * harness only writes its UNIQUE part: the DOM html, any EXTRA globals it
 * touches, the target JS file list, and the assertions.
 *
 * USAGE (from a per-test harness string, run via tests/_jsdom.py):
 *
 *   const { setup } = require(process.env.JSDOM_HARNESS);
 *   const { window, document, check, report } = setup({
 *     root: process.argv[3],                 // repo root (for node_modules + JS_DIR)
 *     html: '<!DOCTYPE html><body><div id="convList"></div></body>',
 *     targets: [process.argv[2]],            // absolute paths to the JS files to eval
 *     globals: { activeStreams: new Map() }, // EXTRA window globals this test needs
 *   });
 *   // ... your assertions ...
 *   check('something', cond);
 *   report();                                // prints PASS/FAIL lines, parsed by python
 *
 * The eval of target files happens in setup()'s scope; because the frontend is
 * pure window-scope concatenation (no modules), every top-level `function`/`var`
 * in a target file becomes a global the assertions can call.
 * ═══════════════════════════════════════════════════════════════════ */
'use strict';

const fs = require('fs');
const path = require('path');

/** Default stubs nearly every harness needs. Override/extend via `globals`. */
function _stdStubs(win) {
  const esc = (s) =>
    String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const stubs = {
    escapeHtml: esc,
    renderMarkdown: (s) => '<p>' + esc(s) + '</p>',
    // t() i18n — echo the key (+ :n when an {n} option is passed) so labels
    // are deterministic and assertions can match on the key.
    t: (k, o) => k + (o && o.n != null ? (':' + o.n) : ''),
    Icon: () => '',
    IconDot: () => '',
    _TOOL_DISPLAY: {},
    // formatClockTime (core.js) — shared HH:MM formatter used by the streaming
    // bubble builders; deterministic stub so time strings don't vary per run.
    formatClockTime: () => '12:00',
    // getConvById / getActiveConv (core.js) — the canonical conversation
    // lookup helpers that several feature modules now delegate to instead of
    // open-coding `conversations.find((c) => c.id === X)`. A harness that
    // evals such a module but doesn't seed `conversations` needs these so the
    // cross-file reference resolves. Tests that DO drive the lookup override
    // via `globals`.
    getConvById: () => null,
    getActiveConv: () => null,
    // CSS.escape is used by querySelector-building code.
    CSS: { escape: (s) => s },
  };
  for (const [k, v] of Object.entries(stubs)) {
    win[k] = global[k] = v;
  }
  // IntersectionObserver — windowing code instantiates it.
  global.IntersectionObserver = win.IntersectionObserver =
    class { observe() {} disconnect() {} unobserve() {} };
  return stubs;
}

/**
 * Spin up jsdom, stub globals, eval the target JS files, and return the
 * reporter helpers.
 *
 * @param {object} opts
 * @param {string} opts.root      Repo root (contains node_modules/jsdom).
 * @param {string} [opts.html]    Initial document body HTML.
 * @param {string[]} [opts.targets] Absolute paths of JS files to eval, in order.
 * @param {object} [opts.globals] Extra window globals to define BEFORE eval.
 * @returns {{window:object, document:object, check:Function, report:Function, out:string[]}}
 */
function setup(opts) {
  const root = opts.root;
  const html = opts.html || '<!DOCTYPE html><body></body>';
  const targets = opts.targets || [];
  const extra = opts.globals || {};

  const { JSDOM } = require(path.join(root, 'node_modules', 'jsdom'));
  const dom = new JSDOM(html, { url: 'http://localhost/' });
  const win = dom.window;
  global.window = win;
  global.document = win.document;
  global.console = console;

  // Neuter tickers + frame callbacks so self-installing timers in the source
  // (swarm panel tickers, deferred finishStream calls, etc.) don't hang node.
  global.setInterval = win.setInterval = () => 0;
  global.setTimeout = win.setTimeout = (fn) => 0;
  global.requestAnimationFrame = win.requestAnimationFrame = () => 0;
  global.getSelection = win.getSelection = () => ({ isCollapsed: true, rangeCount: 0 });

  _stdStubs(win);

  // Apply caller-supplied extra globals (override defaults where they collide).
  for (const [k, v] of Object.entries(extra)) {
    win[k] = global[k] = v;
  }

  // Eval each target file via INDIRECT eval so its top-level `function`/`var`
  // declarations land in GLOBAL scope (direct eval inside this function would
  // make them locals of setup(), invisible to the harness body). The frontend
  // is pure window-scope concatenation, so global is the correct home.
  const indirectEval = eval;  // (0, eval)(...) — runs in global scope
  for (const f of targets) {
    indirectEval(fs.readFileSync(f, 'utf8'));
  }

  const out = [];
  const check = (name, cond) => out.push((cond ? 'PASS ' : 'FAIL ') + name);
  const report = () => console.log(out.join('\n'));
  return { window: win, document: win.document, check, report, out };
}

module.exports = { setup };

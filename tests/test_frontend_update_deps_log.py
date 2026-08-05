"""tests/test_frontend_update_deps_log.py — regression for the self-update
"dependency install failed" card showing the FULL log with a copy button.

WHY
---
When ``pip install`` fails after a pull, the update dialog used to render only
``(b.deps_detail || '').slice(-600)`` — a mangled TAIL of the error — and had
NO way to copy it. The operator (screenshot: "No module named pip") could not
read or paste the whole log. The fix:
  • backend keeps the full log (bounded to 20 KB, not 500 chars);
  • ``_renderDepsFailed`` renders the COMPLETE ``deps_detail`` verbatim inside a
    scrollable ``.upd-log`` block with a copy button (``_copyUpdateLog``) that
    lifts the exact raw bytes from a base64 stash.

This harness loads the REAL shipped update.js under bare node, stubs a minimal
DOM + clipboard, and asserts:
  • the full log text is present (nothing dropped, even a long log);
  • a copy button exists and copies the EXACT raw log bytes;
  • ``_showUpdateError`` renders a log block when a detail is supplied.

NEUTER (on a mutated copy; shipped file untouched): restoring the old
``.slice(-600)`` truncation makes the full-log assertion FAIL — proving the
no-truncation behavior is load-bearing.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


_HARNESS = r"""
const fs = require('fs');
global.window = global;
const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

global.debugLog = () => {};
global.escapeHtml = (s) => String(s)
  .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
global.t = (k) => k;                 // return the key so we can grep it
global.addEventListener = () => {};
global._onReady = () => {};   // feature-loader.js deferred-ready hook (Epic-E sub-9)
global.setTimeout = (fn) => { try { fn && fn(); } catch(e){} return 0; };
global.clearTimeout = () => {};
global.requestAnimationFrame = () => 0;
global.cancelAnimationFrame = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.btoa = (s) => Buffer.from(s, 'binary').toString('base64');
global.atob = (s) => Buffer.from(s, 'base64').toString('binary');

// Capture clipboard writes. NOTE: Node 21+ ships a built-in read-only
// ``navigator`` global, so a plain ``global.navigator = …`` is SILENTLY
// ignored — must defineProperty to override it.
let _clip = null;
Object.defineProperty(global, 'navigator', {
  value: { clipboard: { writeText: (t) => { _clip = t; return Promise.resolve(); } } },
  configurable: true, writable: true,
});

// ── Minimal DOM. A node stores innerHTML verbatim; querySelector/closest walk
//    a parsed shadow of the assigned HTML is overkill — we only need the action
//    area's innerHTML string + a synthetic button/log wiring for the copy test.
function El() {
  const self = {
    _html: '', dataset: {}, _classes: new Set(),
    set innerHTML(v){ self._html = v; },
    get innerHTML(){ return self._html; },
    classList: { add:(c)=>self._classes.add(c), remove:(c)=>self._classes.delete(c),
                 contains:(c)=>self._classes.has(c) },
  };
  return self;
}
const area = El();
global.document = { getElementById: (id) => (id === 'updateActionArea' ? area : null) };

const SRC = fs.readFileSync(process.argv[2], 'utf8');
function loadModule(src){ (0, eval)(src); }
loadModule(SRC);

if (typeof _renderDepsFailed !== 'function' || typeof _updateLogBlockHtml !== 'function'
    || typeof _copyUpdateLog !== 'function' || typeof _showUpdateError !== 'function') {
  console.log('FAIL fns_exposed'); console.log(out.join('\n')); process.exit(0);
}
check('fns_exposed', true);

// A long, multi-line log — much longer than the old 600-char slice — with a
// distinctive FIRST line that a tail-truncation would drop.
let log = 'FIRST_LINE_MARKER: /path/.venv/bin/python: No module named pip\n';
for (let i = 0; i < 400; i++) log += 'noise line ' + i + ' xxxxxxxxxxxxxxxxxxxxxxxxx\n';
log += 'LAST_LINE_MARKER: install aborted';
check('log_is_long', log.length > 5000);

_renderDepsFailed({ new_version: '0.15.2', deps_changed: true, deps_installed: false,
                    deps_detail: log });
const html = area.innerHTML;
// The FULL log must be present — both the first AND last marker survive.
check('deps_full_first_line', html.indexOf('FIRST_LINE_MARKER') >= 0);
check('deps_full_last_line', html.indexOf('LAST_LINE_MARKER') >= 0);
check('deps_has_log_block', html.indexOf('upd-log') >= 0);
check('deps_has_copy_btn', html.indexOf('_copyUpdateLog') >= 0);
check('deps_still_has_restart', html.indexOf('updateRestartBtn') >= 0);

// ── Copy button lifts the EXACT raw bytes (from the base64 stash) ──
// Reconstruct the .upd-log wrapper the button lives in, mimicking closest().
const stash = global.btoa(unescape(encodeURIComponent(log)));
const logWrap = { dataset: { log: stash }, querySelector: () => null };
const btn = {
  classList: { add:()=>{}, remove:()=>{} },
  closest: (sel) => (sel === '.upd-log' ? logWrap : null),
  querySelector: () => ({ textContent: '' }),
};
_clip = null;
_copyUpdateLog(btn);
check('copy_exact_bytes', _clip === log);

// ── _showUpdateError with a detail renders a log block too ──
_showUpdateError('Update failed unexpectedly.', 'DETAIL_MARKER: traceback here');
const ehtml = area.innerHTML;
check('err_shows_msg', ehtml.indexOf('Update failed unexpectedly.') >= 0);
check('err_shows_detail_log', ehtml.indexOf('DETAIL_MARKER') >= 0 && ehtml.indexOf('upd-log') >= 0);

// _showUpdateError with NO detail → no log block (unchanged behavior).
_showUpdateError('plain error');
check('err_no_detail_no_log', area.innerHTML.indexOf('upd-log') < 0);

// ── NEUTER: reinstate the old 600-char tail truncation → full log FAILS ──
{
  const NEEDLE = "  const text = String(logText || '');\n  if (!text) return '';";
  const neutered = SRC.replace(NEEDLE,
    "  let text = String(logText || '').slice(-600);\n  if (!text) return '';");
  check('neuter_applied', neutered !== SRC);
  loadModule(neutered);
  const a2 = area; a2.innerHTML = '';
  _renderDepsFailed({ new_version: '0.15.2', deps_changed: true, deps_installed: false,
                      deps_detail: log });
  // The first-line marker is >600 chars from the end → truncated away.
  check('neuter_drops_first_line', a2.innerHTML.indexOf('FIRST_LINE_MARKER') < 0);
}

console.log(out.join('\n'));
process.exit(0);
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_update_deps_failed_full_log_and_copy():
    harness = os.path.join(HERE, '_update_deps_log_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'update.js')],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [l for l in output.splitlines() if l.startswith('FAIL')]
    assert not fails, 'update deps-log failures:\n' + output
    assert output.count('PASS') >= 13, f'expected >=13 PASS lines, got:\n{output}'

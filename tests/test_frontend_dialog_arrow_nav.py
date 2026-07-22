"""tests/test_frontend_dialog_arrow_nav.py — keyboard-only navigation of the
themed confirm dialog (``showConfirm`` in ``static/js/core/dialog.js``).

The confirm dialog (used e.g. by the log-noise "Clean & send" / "Keep original"
prompt) must be usable without a mouse:

  * Left / Right arrow keys move the selection between Cancel (left) and OK
    (right).
  * Enter activates the CURRENTLY-FOCUSED button — so a user who arrowed to
    Cancel and pressed Enter gets the cancel result, not OK.

This drives the REAL shipped ``showConfirm`` under jsdom. A byte-reverting
NEUTER (Enter always resolves OK regardless of focus) proves the focus-aware
Enter branch is load-bearing.

Skips cleanly when node + jsdom aren't installed.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/test_frontend_dialog_arrow_nav.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[1];
const NEUTER = process.argv[2] === 'neuter';
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM('<!DOCTYPE html><body></body>', { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.requestAnimationFrame = win.requestAnimationFrame = (fn) => setTimeout(fn, 0);
global.t = win.t = (k) => k;

let dialogSrc = fs.readFileSync(
  path.join(ROOT, 'static', 'js', 'core', 'dialog.js'), 'utf8');

if (NEUTER) {
  // NEUTER: Enter ignores which button is focused and always confirms OK.
  // With this, arrowing to Cancel then pressing Enter must WRONGLY yield OK.
  dialogSrc = dialogSrc.replace(
    'if (cancelBtn && document.activeElement === cancelBtn) close(cancelResult);\n        else close(okResult);',
    'close(okResult);');
}
eval(dialogSrc);   // defines showConfirm etc.

function key(k) {
  const ev = new win.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true });
  win.document.dispatchEvent(ev);
}

(async () => {
  const out = {};

  // ── Scenario 1: ArrowLeft focuses Cancel, ArrowRight focuses OK ──
  {
    const p = showConfirm('proceed?', { okText: 'Clean', cancelText: 'Keep' });
    await new Promise((r) => setTimeout(r, 10));   // let rAF focus OK
    const ok = win.document.querySelector('.app-dialog-ok');
    const cancel = win.document.querySelector('.app-dialog-cancel');
    out.default_focus_ok = win.document.activeElement === ok;
    key('ArrowLeft');
    out.left_focuses_cancel = win.document.activeElement === cancel;
    key('ArrowRight');
    out.right_focuses_ok = win.document.activeElement === ok;
    // Now go back to Cancel and confirm with Enter.
    key('ArrowLeft');
    key('Enter');
    out.enter_on_cancel = await p;   // expect false
  }
  win.document.querySelectorAll('.app-dialog-overlay').forEach((o) => o.remove());

  // ── Scenario 2: default (no arrow) Enter still confirms OK ──
  {
    const p = showConfirm('proceed?', { okText: 'Clean', cancelText: 'Keep' });
    await new Promise((r) => setTimeout(r, 10));
    key('Enter');
    out.enter_default = await p;   // expect true
  }
  win.document.querySelectorAll('.app-dialog-overlay').forEach((o) => o.remove());

  console.log(JSON.stringify(out));
  process.exit(0);
})().catch((e) => { console.log(JSON.stringify({ error: String(e && e.stack || e) })); process.exit(0); });
"""


def _run(neuter: bool = False) -> dict:
    arg = 'neuter' if neuter else 'normal'
    proc = subprocess.run(
        ['node', '-e', _HARNESS, ROOT, arg],
        capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert proc.returncode == 0, f'node harness failed: {proc.stderr[:2000]}'
    line = [ln for ln in proc.stdout.strip().splitlines() if ln.startswith('{')][-1]
    return json.loads(line)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_arrow_keys_move_selection():
    r = _run()
    assert 'error' not in r, r.get('error')
    assert r['default_focus_ok'] is True, 'OK button should be focused on open'
    assert r['left_focuses_cancel'] is True, 'ArrowLeft must focus the Cancel button'
    assert r['right_focuses_ok'] is True, 'ArrowRight must focus the OK button'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_enter_activates_focused_button():
    r = _run()
    assert 'error' not in r, r.get('error')
    assert r['enter_on_cancel'] is False, \
        'Enter after arrowing to Cancel must resolve the cancel result (false)'
    assert r['enter_default'] is True, \
        'Enter with default OK focus must resolve the ok result (true)'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NEUTER_enter_ignores_focus():
    """Byte-reverting NEUTER: make Enter always confirm OK regardless of focus.
    Arrowing to Cancel then pressing Enter now WRONGLY yields OK, proving the
    focus-aware Enter branch is load-bearing."""
    r = _run(neuter=True)
    assert 'error' not in r, r.get('error')
    assert r['enter_on_cancel'] is True, (
        'NEUTER: without focus-aware Enter, confirming on Cancel wrongly '
        'resolves OK (true)')


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))

"""Regression test: background/async toasts (external-file-edit capture and
workspace-root auto-registration) must be CONVERSATION-AWARE and ACTIONABLE.

WHY
---
Both events fire from a specific task's SSE stream — often a BACKGROUND
conversation, not the one on screen. The old toasts were English-only,
emoji-prefixed, named no source conversation, and offered no next step
("Captured N external edit(s) — a.py, b.py"). A user seeing it had no idea
WHICH chat triggered it or WHAT to do.

The fix:
  * ``showToast(icon, title, detail, dur, opts)`` gained an ``opts`` object
    that renders a clickable "from «conv title»" source badge (jumps to that
    conversation via ``loadConversation``) plus a quieter guidance ``hint``.
  * ``_handleProjectExternalEdit`` / ``_handleWorkspaceRootAdded`` now pass the
    source conv + a localized, valuable hint.

This drives the REAL shipped ``escape_html.js`` + ``i18n.js`` + ``toast.js`` +
``sse_handlers_misc.js`` under jsdom. Skips cleanly when node + jsdom aren't
installed.
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


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[2];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div id="toastContainer"></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
global.navigator = win.navigator;
global.localStorage = win.localStorage || { getItem: () => null, setItem() {}, removeItem() {} };
global.setTimeout = win.setTimeout = (fn) => 0;   // neuter auto-dismiss timers
global.clearTimeout = win.clearTimeout = () => {};

// State the handlers read.
let conversations = [
  { id: 'c-bg', title: 'Nightly refactor run' },
];
win.conversations = global.conversations = conversations;
Object.defineProperty(win, 'activeConvId', { get: () => 'c-front', set() {} });
global.activeConvId = 'c-front';

// Capture navigation.
let _navTo = null;
win.loadConversation = global.loadConversation = (id) => { _navTo = id; };
// Api used by the workspace-root state-parity branch — gate it off (inactive conv).
win.Api = global.Api = { project: { status: () => Promise.resolve(null) } };
win.getActiveConv = global.getActiveConv = () => null;
win.saveConversations = global.saveConversations = () => {};
win.syncConversationToServer = global.syncConversationToServer = () => {};
win._applyProjectData = global._applyProjectData = () => {};
win.renderConversationList = global.renderConversationList = () => {};
win.twUpdate = global.twUpdate = () => {};
win.updateContextBar = global.updateContextBar = () => {};

// Load REAL shipped files (escape_html + i18n + toast + handlers).
eval(fs.readFileSync(process.argv[3], 'utf8'));  // core/escape_html.js
eval(fs.readFileSync(process.argv[4], 'utf8'));  // i18n.js
eval(fs.readFileSync(process.argv[5], 'utf8'));  // core/toast.js
eval(fs.readFileSync(process.argv[6], 'utf8'));  // ui/sse_handlers_misc.js

// Ensure english so assertions are language-stable.
try { _i18nLang = 'en'; } catch (_) {}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function lastToast() {
  const c = document.getElementById('toastContainer');
  return c.lastElementChild;
}
function ctx() {
  return { convId: 'c-bg', taskId: 't1', assistantMsg: {}, buf: {} };
}

// ── seam present ──
check('showToast_exposed', typeof showToast === 'function');
check('handlers_exposed', typeof _handleProjectExternalEdit === 'function' &&
  typeof _handleWorkspaceRootAdded === 'function');

// ── 1. external-edit toast: source badge + hint + navigable + files ──
{
  _handleProjectExternalEdit({ files: ['src/a.py', 'src/b.py'], sha: 'deadbeef1234' }, ctx());
  const el = lastToast();
  const html = el ? el.innerHTML : '';
  check('ee_toast_rendered', !!el);
  check('ee_has_conv_src', html.includes('toast-conv-src'));
  check('ee_names_source_conv', html.includes('Nightly refactor run'));
  check('ee_has_hint', html.includes('toast-hint') &&
    /file history/i.test(html));
  check('ee_lists_files', html.includes('src/a.py') && html.includes('src/b.py'));
  check('ee_shows_sha', html.includes('deadbee'));  // sha sliced to 7 chars
  check('ee_is_clickable', el.classList.contains('toast-clickable'));
  // No raw emoji leaked into the visible text.
  check('ee_no_emoji', !/\uD83D\uDCDD/.test(html));  // 📝
  // Clicking the toast jumps to the SOURCE conversation.
  _navTo = null;
  el.dispatchEvent(new win.Event('click'));
  check('ee_click_navigates', _navTo === 'c-bg');
}

// ── 2. clicking the CLOSE button must NOT navigate ──
{
  document.getElementById('toastContainer').innerHTML = '';
  _handleProjectExternalEdit({ files: ['x.py'], sha: 'abc' }, ctx());
  const el = lastToast();
  _navTo = null;
  const closeBtn = el.querySelector('.toast-close');
  closeBtn.dispatchEvent(new win.Event('click', { bubbles: true }));
  check('ee_close_no_navigate', _navTo === null);
}

// ── 3. workspace-root toast: source badge + explanatory hint ──
{
  document.getElementById('toastContainer').innerHTML = '';
  _handleWorkspaceRootAdded({ roots: [{ rootName: 'libfoo', path: '/abs/libfoo' }] },
    { convId: 'c-bg' });
  const el = lastToast();
  const html = el ? el.innerHTML : '';
  check('wr_toast_rendered', !!el);
  check('wr_names_source_conv', html.includes('Nightly refactor run'));
  check('wr_has_hint', html.includes('toast-hint') && /workspace root/i.test(html));
  check('wr_names_root', html.includes('libfoo'));
  check('wr_is_clickable', el.classList.contains('toast-clickable'));
}

// ── 4. unknown conv id → still renders a source badge with a fallback title ──
{
  document.getElementById('toastContainer').innerHTML = '';
  _handleProjectExternalEdit({ files: ['y.py'], sha: '' },
    { convId: 'c-missing', taskId: 't', assistantMsg: {}, buf: {} });
  const el = lastToast();
  const html = el ? el.innerHTML : '';
  check('missing_conv_has_badge', html.includes('toast-conv-src'));
  check('missing_conv_fallback_title', html.includes('Untitled chat'));
}

// ── 5. NEUTER: strip the opts wiring from showToast → the badge/hint vanish
//        and the toast is no longer navigable. Proves opts is load-bearing. ──
{
  document.getElementById('toastContainer').innerHTML = '';
  const _real = showToast;
  // Simulate the PRE-FIX showToast (ignores the 5th opts arg entirely).
  // The handler resolves the LEXICAL `showToast` binding (direct-eval scope),
  // so we must reassign the bare identifier — not just a global-object prop.
  showToast = function(icon, titleOrType, detail, dur) {
    return _real(icon, titleOrType, detail, dur /* , NO opts */);
  };
  _navTo = null;
  _handleProjectExternalEdit({ files: ['z.py'], sha: 'ff' }, ctx());
  const el = lastToast();
  const html = el ? el.innerHTML : '';
  check('neuter_no_conv_src', !html.includes('toast-conv-src'));
  check('neuter_no_hint', !html.includes('toast-hint'));
  el.dispatchEvent(new win.Event('click'));
  check('neuter_not_navigable', _navTo === null);
  showToast = _real;   // restore
}

// ── 6. plain simple-form toast is unaffected (no badge/hint, not clickable) ──
{
  document.getElementById('toastContainer').innerHTML = '';
  showToast('Just a message', 'success');
  const el = lastToast();
  const html = el ? el.innerHTML : '';
  check('plain_no_conv_src', !html.includes('toast-conv-src'));
  check('plain_no_hint', !html.includes('toast-hint'));
  check('plain_not_clickable', !el.classList.contains('toast-clickable'));
  check('plain_shows_text', html.includes('Just a message'));
}

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_toast_conv_source_and_hint():
    harness = os.path.join(HERE, '_toast_conv_source_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             ROOT,                                                    # argv[2]
             os.path.join(JS_DIR, 'core', 'escape_html.js'),          # argv[3]
             os.path.join(JS_DIR, 'i18n.js'),                         # argv[4]
             os.path.join(JS_DIR, 'core', 'toast.js'),                # argv[5]
             os.path.join(JS_DIR, 'ui', 'sse_handlers_misc.js'),      # argv[6]
             ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'toast conv-source failures:\n' + output
    assert output.count('PASS') >= 22, f'expected >=22 PASS lines, got:\n{output}'

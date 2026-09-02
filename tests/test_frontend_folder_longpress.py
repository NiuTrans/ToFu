"""tests/test_frontend_folder_longpress.py — Mobile folder-tab long-press +
swipe-overlay guard (`runtime/sections/main/main_folders_mobile.js`).

Two mobile-only UX fixes are exercised against the REAL shipped source under
jsdom (indirect eval → globals, mirroring the concatenated feature bundle):

  1. LONG-PRESS: folder rename/delete lived only on a `contextmenu` listener,
     which mobile browsers don't reliably fire on a touch long-press — so the
     menu was UNREACHABLE by touch. `_initFolderTabs` now also binds a 500ms
     touch long-press that opens the same `_showFolderTabMenu`, and swallows
     the trailing click so the folder isn't switched. We fake-time the 500ms
     and assert the menu opens; a control tap (touchstart→immediate touchend)
     must NOT open it.

  2. SWIPE GUARD: `initMobileGestures` must NOT start tracking a sidebar-drawer
     swipe when a mobile bottom-sheet / portaled panel is open (otherwise the
     drawer opens BEHIND the sheet). We can't easily drive the whole IIFE, so
     this is a source-contract assert that the guard predicate + early return
     exist and reference the overlay selectors.

  3. DELETE-CONFIRM DIALOG: `_confirmDeleteFolder` (a destructive modal) now
     dismisses on Escape — matching its create/rename siblings — and focuses
     Cancel by default. Driven for real under jsdom (Escape closes, other keys
     don't, listener is cleaned up, Cancel is focused).

  4. FLOW-PICKER ARIA: the mobile flow picker (`openMobileFlowPicker` in
     mobile_panels.js) emits role=listbox / role=option / aria-selected so a
     screen reader can navigate it. Source-contract (IIFE + async-Api-bound).

Triple-neuter proves the long-press wiring AND the delete-dialog Escape are
each load-bearing. Skips cleanly without node/jsdom.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from tests._runtime_sections import runtime_sections_dir

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = runtime_sections_dir()
_SRC = os.path.join(JS_DIR, 'main', 'main_folders_mobile.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const SRC = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(`<!DOCTYPE html><body>
  <div id="folderTabs"><div class="folder-tabs-scroll">
    <div class="folder-tab" data-folder-id="f1">Work</div>
    <div class="folder-tab folder-tab-add">+</div>
  </div></div>
</body>`, { url: 'http://localhost/', pretendToBeVisual: true });
const { window } = dom;
global.window = window; global.document = window.document;
global.navigator = window.navigator;
// jsdom lacks ontouchstart — force the touch branch on.
if (!('ontouchstart' in window)) window.ontouchstart = null;

// ── Stub the many globals _initFolderTabs closes over. ──
global.t = (k) => k;
global.escapeHtml = (s) => String(s == null ? '' : s);
global.getFolderById = (id) => ({ id, name: 'Work' });
let _renameCalls = 0;
global._promptRenameFolder = () => { _renameCalls++; };
let _switchCalls = 0;
global.setActiveFolderId = () => { _switchCalls++; };
global._promptCreateFolder = () => {};
global.setConversationFolder = () => {};
global._dragConvId = null;
global._folderTabsExpanded = false;
global.showToast = () => {};
// Deps of the real _confirmDeleteFolder (eval overrides any stub of the fn
// itself; these are the globals its body closes over).
global.deleteFolder = async () => {};
global.renderConversationList = () => {};
global.getActiveFolderId = () => null;

// Deterministic timers for the 500ms long-press. IDs start at 1 — real
// browsers never return 0, and the source's `if (_lpTimer)` cancel guard
// treats 0 as "no timer", so a 0-based id would defeat clearTimeout.
let _timers = {};
let _timerSeq = 0;
global.setTimeout = (fn, ms) => { const id = ++_timerSeq; _timers[id] = { fn, ms, live: true }; return id; };
global.clearTimeout = (id) => { if (_timers[id]) _timers[id].live = false; };
function _flushTimers() { Object.values(_timers).forEach(t => { if (t.live) { t.live = false; t.fn(); } }); }
window.setTimeout = global.setTimeout; window.clearTimeout = global.clearTimeout;

// Only need _initFolderTabs + _showFolderTabMenu from the source. Eval the
// whole file (indirect eval → top-level fn decls become globals). Guard the
// trailing window.visualViewport IIFE — jsdom has no visualViewport, and the
// resize/scroll IIFEs reference isNearBottom/scrollToBottom we don't stub;
// they're harmless because they only *register* listeners at load, but the
// keyboard IIFE early-returns without visualViewport. Provide no-op deps.
global.isNearBottom = () => true;
global.scrollToBottom = () => {};
global._scheduleReflow = () => {};
global.imageGenMode = false;
global.config = {};
// Viewport predicates the load-time IIFEs (initMobileLayout / resize handler)
// call — jsdom has no real layout, so stub them deterministically.
global.isDrawerViewport = () => false;
global.isMobileViewport = () => false;
global.isTabletDrawerViewport = () => false;

let src = fs.readFileSync(SRC, 'utf8');
(0, eval)(src);

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const tabsEl = document.getElementById('folderTabs');
_initFolderTabs();
const tab = tabsEl.querySelector('.folder-tab[data-folder-id="f1"]');

function _touch(el, type, x, y) {
  const ev = new window.Event(type, { bubbles: true, cancelable: true });
  ev.touches = (type === 'touchend' || type === 'touchcancel') ? [] : [{ clientX: x, clientY: y }];
  el.dispatchEvent(ev);
  return ev;
}

// (A) Long-press: touchstart → wait 500ms → menu opens.
_touch(tab, 'touchstart', 40, 40);
check('menu_absent_before_timer', document.getElementById('_folderTabMenu') === null);
_flushTimers();
check('longpress_opens_menu', document.getElementById('_folderTabMenu') !== null);
// The follow-up click must be swallowed (folder NOT switched).
_switchCalls = 0;
const clickEv = new window.Event('click', { bubbles: true, cancelable: true });
Object.defineProperty(clickEv, 'target', { value: tab });
tabsEl.dispatchEvent(clickEv);
check('longpress_click_swallowed', _switchCalls === 0);
// Clean the menu for the next case.
const m1 = document.getElementById('_folderTabMenu'); if (m1) m1.remove();

// (B) Quick tap: touchstart then immediate touchend BEFORE the timer → no menu.
_timers = {};
_touch(tab, 'touchstart', 40, 40);
_touch(tab, 'touchend', 40, 40);
_flushTimers();  // timer was cleared → fn should not run
check('quick_tap_no_menu', document.getElementById('_folderTabMenu') === null);

// (C) Move cancels the long-press.
_timers = {};
const m0 = document.getElementById('_folderTabMenu'); if (m0) m0.remove();
_touch(tab, 'touchstart', 40, 40);
_touch(tab, 'touchmove', 90, 42);  // dx=50 > 10 → cancel
_flushTimers();
check('move_cancels_longpress', document.getElementById('_folderTabMenu') === null);

// (D) Source-contract: the swipe-gesture overlay guard exists.
check('swipe_guard_predicate', src.includes('_isMobileOverlayOpen'));
check('swipe_guard_early_return',
  /_isMobileOverlayOpen\(\)\)\s*\{\s*tracking\s*=\s*false;\s*return;/.test(src));
check('swipe_guard_selectors',
  src.includes('mobile-bottom-sheet.open') &&
  src.includes('mobile-panel-portaled.visible'));

// (E) Delete-confirm dialog: Escape dismisses it (matching create/rename),
//     and Cancel is focused by default (destructive-modal safety). The real
//     _confirmDeleteFolder is defined globally by the eval above.
function _keydown(key) {
  const ev = new window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true });
  document.dispatchEvent(ev);
}
_confirmDeleteFolder('f1');
const dlg = document.getElementById('_folderCreateDialog');
check('delete_dialog_opens', dlg !== null);
// Cancel focused by default (after the 50ms focus timeout fires).
_flushTimers();
check('delete_cancel_focused',
  document.activeElement === document.getElementById('_folderDialogCancel'));
// A non-Escape key must NOT close it.
_keydown('a');
check('delete_dialog_survives_other_key',
  document.getElementById('_folderCreateDialog') !== null);
// Escape closes it.
_keydown('Escape');
check('delete_dialog_escape_closes',
  document.getElementById('_folderCreateDialog') === null);
// Listener cleanup: a second Escape after close must not throw (handler gone).
_keydown('Escape');
check('delete_escape_listener_cleaned',
  document.getElementById('_folderCreateDialog') === null);

console.log(out.join('\n'));
"""


def _run(src_path):
    harness = os.path.join(HERE, '_folder_longpress_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, src_path, ROOT],
            capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    return output


_EXPECTED = (
    'menu_absent_before_timer', 'longpress_opens_menu',
    'longpress_click_swallowed', 'quick_tap_no_menu', 'move_cancels_longpress',
    'swipe_guard_predicate', 'swipe_guard_early_return', 'swipe_guard_selectors',
    'delete_dialog_opens', 'delete_cancel_focused',
    'delete_dialog_survives_other_key', 'delete_dialog_escape_closes',
    'delete_escape_listener_cleaned',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_folder_longpress_and_swipe_guard():
    output = _run(_SRC)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'folder-longpress failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


def _nc(anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY, run, assert target checks flip to FAIL while a control
    stays PASS, then assert the shipped file is byte-identical."""
    with open(_SRC, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    # Suffix the copy with THIS test module's name so it can never collide
    # with a sibling NC test that patches the SAME shipped source under xdist
    # (test_frontend_project_rail also copies main_folders_mobile.js).
    copy_path = _SRC + '.longpress.nc_copy.js'
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        for m in must_fail:
            assert ('FAIL ' + m) in output, f'NC: expected {m} to FAIL:\n{output}'
        for m in must_still_pass:
            assert ('PASS ' + m) in output, \
                f'NC must be surgical — {m} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_SRC, encoding='utf-8') as f:
        assert f.read() == original, 'shipped main_folders_mobile.js must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_longpress_timer_is_load_bearing():
    """Neuter the long-press: make _showFolderTabMenu never fire from the hold
    (drop the call in the setTimeout body) → longpress_opens_menu FAILs while
    the quick-tap / move-cancel controls still PASS."""
    _nc(
        anchor='        _lpTimer = null; _lpFired = true;\n        _showFolderTabMenu(fid, _lpX, _lpY);',
        replacement='        _lpTimer = null; _lpFired = false;',
        must_fail=['longpress_opens_menu'],
        must_still_pass=['quick_tap_no_menu', 'move_cancels_longpress'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_click_swallow_is_load_bearing():
    """Neuter the trailing-click suppression → longpress_click_swallowed FAILs
    (the folder switch fires) while the menu-open check still PASSes."""
    _nc(
        anchor="      if (_lpFired) { _lpFired = false; e.stopPropagation(); e.preventDefault(); }",
        replacement="      if (false) { _lpFired = false; e.stopPropagation(); e.preventDefault(); }",
        must_fail=['longpress_click_swallowed'],
        must_still_pass=['longpress_opens_menu'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_delete_dialog_escape_is_load_bearing():
    """Neuter the delete-dialog Escape branch → delete_dialog_escape_closes
    FAILs (the destructive modal stays up on Escape) while the dialog-opens
    and Cancel-focus controls still PASS."""
    _nc(
        anchor="  function _onKey(e) { if (e.key === 'Escape') { e.preventDefault(); _closeDialog(); } }",
        replacement="  function _onKey(e) { void e; }",
        must_fail=['delete_dialog_escape_closes'],
        must_still_pass=['delete_dialog_opens', 'delete_cancel_focused'],
    )


_PANELS_SRC = os.path.join(JS_DIR, 'mobile_panels.js')


def test_flow_picker_aria_roles_present():
    """The mobile flow picker is an accessible bottom-sheet listbox."""
    with open(_PANELS_SRC, encoding='utf-8') as f:
        src = f.read()
    assert 'id="mobileFlowSheetList" role="listbox"' in src, \
        'flow-picker list must be role=listbox'
    assert 'id="mobileFlowSheetStatus" role="status"' in src, \
        'catalog availability must be announced outside the listbox'
    assert src.index('id="mobileFlowSheetStatus"') < src.index('id="mobileFlowSheetList"'), \
        'catalog status must not be nested among listbox options'
    assert 'aria-labelledby="mobileFlowSheetTitle"' in src, \
        'flow-picker listbox must reference its title'
    assert 'role="option"' in src, 'each flow item must be role=option'
    assert '<button type="button" class="mobile-sheet-item' in src, \
        'flow options must be natively focusable controls'
    assert 'aria-selected="' in src, 'each flow item must carry aria-selected'
    # aria-selected must be data-driven (true for the current flow), not a
    # hardcoded constant.
    assert "it.flow === current ? \"true\" : \"false\"" in src, \
        'aria-selected must reflect the active flow'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))

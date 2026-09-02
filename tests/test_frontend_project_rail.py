"""tests/test_frontend_project_rail.py — Vertical project-rail redesign guard.

WHY
---
The old wrapping pill tab-bar (`.folder-tabs-scroll` flex-wrap) was replaced by
a vertical project RAIL: a left column of full-width project rows in
`runtime/sections/ui/conversation_list.js` (`_renderFolderTabsInner`) plus the
collapse/drag interactions in `runtime/sections/main/main_folders_mobile.js`
(`_initFolderTabs` / `_toggleProjectRail`).

Both REAL shipped files are eval'd under jsdom (indirect eval → window-scope
globals, mirroring the concatenated feature bundle) and driven to assert the
behaviors the owner held me to:

  • RENDER — with ≥1 folder the rail emits a `.project-rail-list` of
    `.folder-tab[data-folder-id]` rows (未分类 + one per folder + a footer
    add-row) and the sidebar gains `.has-rail`.
  • ZERO-FOLDER DEGRADATION — with 0 folders the rail is EMPTY and `.has-rail`
    is absent (single-column list preserved; quick-add is the only entry point).
  • FILTER — selecting a project filters the conversation list to that
    project's conversations.
  • FAST PATH — re-selecting the active project swaps the `.active` class in
    place (no `.project-rail-list` node-identity change / no rebuild).
  • DRAG — dropping a dragged conversation onto a project row calls
    `setConversationFolder(convId, folderId)`.
  • COLLAPSE PERSIST — `_toggleProjectRail()` toggles `.rail-collapsed` on the
    sidebar AND persists the choice to localStorage (read back by the renderer).

Triple-neuter proves the render, filter, and drag paths are each load-bearing.
Skips cleanly without node/jsdom.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest
from tests._runtime_sections import orchestration_legacy_test_root as _legacy_test_root

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = _legacy_test_root()
JS_DIR = os.path.join(ROOT, 'static', 'js')
_CONV = os.path.join(JS_DIR, 'ui', 'conversation_list.js')
_FOLDERS = os.path.join(JS_DIR, 'main', 'main_folders_mobile.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const CONV = process.argv[2];
const ROOT = process.argv[3];
const FOLDERS = process.argv[4];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM(`<!DOCTYPE html><body>
  <aside class="sidebar" id="sidebar">
    <button class="folder-quickadd" id="folderQuickAdd"></button>
    <div class="sidebar-body" id="sidebarBody">
      <nav class="project-rail" id="folderTabs"></nav>
      <div class="conversations-list" id="convList"></div>
    </div>
    <div id="sidebarSearchStats"></div>
  </aside>
</body>`, { url: 'http://localhost/', pretendToBeVisual: true });
const { window } = dom;
global.window = window; global.document = window.document;
global.navigator = window.navigator;
global.localStorage = window.localStorage;

// ── i18n / html helpers ──
global.t = (k) => k;
global.escapeHtml = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.CSS = window.CSS || { escape: (s) => s };
if (!window.CSS) window.CSS = global.CSS;

// ── Folder + conversation state (driven per-scenario) ──
let _FOLDERS_STATE = [];
let _ACTIVE_FOLDER = null;
global.getFolders = () => _FOLDERS_STATE;
global.getActiveFolderId = () => _ACTIVE_FOLDER;
let _setActiveCalls = [];
global.setActiveFolderId = (id) => { _ACTIVE_FOLDER = id || null; _setActiveCalls.push(id); };
global.areFoldersLoaded = () => true;
global.getFolderById = (id) => _FOLDERS_STATE.find(f => f.id === id) || null;
let _setFolderCalls = [];
global.setConversationFolder = (convId, folderId) => { _setFolderCalls.push([convId, folderId]); };
global._promptCreateFolder = () => {};
global.showToast = () => {};
global.renderConversationList = renderConversationListWrap;  // late-bound below

// Conversation-list render deps (mirrors test_frontend_conv_list_collapse).
global.sidebarSearchQuery = '';
global.formatRelativeTime = () => '';
global.highlightMatch = (s) => s;
global.stripNoTranslateTags = (s) => s;
global.activeStreams = new Map();
global.pendingMessageQueue = new Map();
global.streamBufs = new Map();
global._isDebug = false;
global.BASE_PATH = '';
global.activeConvId = null;

// Viewport predicates the load-time IIFEs in main_folders_mobile.js call.
global.isDrawerViewport = () => false;
global.isMobileViewport = () => false;
global.isTabletDrawerViewport = () => false;
global.isNearBottom = () => true;
global.scrollToBottom = () => {};
global._scheduleReflow = () => {};
global.imageGenMode = false;
global.config = {};
// jsdom lacks IntersectionObserver — windowing code instantiates it.
global.IntersectionObserver = window.IntersectionObserver =
  class { observe(){} disconnect(){} unobserve(){} };
global.setTimeout = window.setTimeout = (fn) => 0;
global.requestAnimationFrame = window.requestAnimationFrame = (fn) => 0;

function renderConversationListWrap() { return _rcl(); }

// ── Eval the two REAL source files (indirect eval → globals). ──
let _rcl = () => {};
(0, eval)(fs.readFileSync(CONV, 'utf8'));
(0, eval)(fs.readFileSync(FOLDERS, 'utf8'));
_rcl = renderConversationList;         // real fn now defined globally
global.renderConversationList = _rcl;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const sidebar = document.getElementById('sidebar');
const tabsEl = document.getElementById('folderTabs');
const convList = document.getElementById('convList');

// Wire the rail interactions once (drag / collapse / click).
_initFolderTabs();
if (typeof _initFolderDragDrop === 'function') _initFolderDragDrop();

// ══ Scenario A: ≥1 folder → vertical rail renders. ══
_FOLDERS_STATE = [
  { id: 'f1', name: 'cadtrans', color: '#3b82f6', order: 0 },
  { id: 'f2', name: 'auto-verifier', color: '#10b981', order: 1 },
];
_ACTIVE_FOLDER = null;
global.conversations = window.conversations = [
  { id: 'c1', title: 'A', _serverTurnCount: 1, updatedAt: 100, folderId: 'f1' },
  { id: 'c2', title: 'B', _serverTurnCount: 1, updatedAt: 200, folderId: 'f1' },
  { id: 'c3', title: 'C', _serverTurnCount: 1, updatedAt: 300, folderId: 'f2' },
  { id: 'c4', title: 'D', _serverTurnCount: 1, updatedAt: 400 },   // uncategorized
];
window._lastConvListHash = '';
renderConversationList();

check('rail_has_list', !!tabsEl.querySelector('.project-rail-list'));
const rows = tabsEl.querySelectorAll('.folder-tab[data-folder-id]');
// 未分类 (empty id) + f1 + f2 = 3 project rows (the add-row has no data-folder-id)
check('rail_row_count', rows.length === 3);
check('rail_has_uncat', !!tabsEl.querySelector('.folder-tab[data-folder-id=""]'));
check('rail_has_f1', !!tabsEl.querySelector('.folder-tab[data-folder-id="f1"]'));
check('rail_has_add', !!tabsEl.querySelector('.folder-tab.folder-tab-add'));
check('rail_f1_count_badge',
  (tabsEl.querySelector('.folder-tab[data-folder-id="f1"] .folder-tab-count')||{}).textContent === '2');
check('sidebar_has_rail_class', sidebar.classList.contains('has-rail'));
// Vertical, not the old wrapping pill scroll.
check('no_legacy_pill_scroll', !tabsEl.querySelector('.folder-tabs-scroll'));
// Monogram tile: each project dot carries a 1–2 char recognition monogram
// (not a lone letter) + a data-mono-len sizing hint. "cadtrans" (single
// word) → "CA"; "auto-verifier" (hyphenated) → "AV".
const f1Dot = tabsEl.querySelector('.folder-tab[data-folder-id="f1"] .folder-tab-dot');
const f2Dot = tabsEl.querySelector('.folder-tab[data-folder-id="f2"] .folder-tab-dot');
check('mono_f1_two_char', !!f1Dot && f1Dot.getAttribute('data-initial') === 'CA');
check('mono_f2_initials', !!f2Dot && f2Dot.getAttribute('data-initial') === 'AV');
check('mono_len_hint', !!f1Dot && f1Dot.getAttribute('data-mono-len') === '2');
// Tile color: an EXPLICIT folder color is honored verbatim; an UNCOLORED
// folder gets a stable, per-key HSL (not the shared var(--accent)), so N
// uncolored projects are visually distinct. Same key → same color (pure).
check('color_explicit_honored', !!f1Dot && f1Dot.style.background === 'rgb(59, 130, 246)');
check('color_uncolored_is_hsl',
  _folderColor({ id: 'x1', name: 'nope' }).startsWith('hsl('));
check('color_uncolored_not_accent',
  _folderColor({ id: 'x1', name: 'nope' }) !== 'var(--accent)');
check('color_deterministic',
  _folderColor({ id: 'x1' }) === _folderColor({ id: 'x1' }));
check('color_distinct_keys',
  _folderColor({ id: 'x1' }) !== _folderColor({ id: 'zzz9' }));

// ══ Scenario B: filter by active project ══
_ACTIVE_FOLDER = 'f1';
window._lastConvListHash = '';
renderConversationList();
const shownB = [...convList.querySelectorAll('.conv-item[data-conv-id]')].map(r => r.dataset.convId).sort();
check('filter_shows_only_f1', shownB.join(',') === 'c1,c2');
check('filter_active_row_marked',
  tabsEl.querySelector('.folder-tab[data-folder-id="f1"]').classList.contains('active'));

// ══ Scenario C: active-only FAST PATH — re-render with a different active
//    project must NOT rebuild the rail list (same node identity), just swap
//    the .active class. ══
const listNodeBefore = tabsEl.querySelector('.project-rail-list');
const f1RowBefore = tabsEl.querySelector('.folder-tab[data-folder-id="f1"]');
_ACTIVE_FOLDER = 'f2';
// NB: do NOT reset _lastConvListHash — folder-tab hash fast-path keys on its
// own _lastFolderTabsHash; renderFolderTabs is called every renderConversationList.
window._lastConvListHash = '';
renderConversationList();
const listNodeAfter = tabsEl.querySelector('.project-rail-list');
check('fastpath_list_node_reused', listNodeBefore === listNodeAfter);
check('fastpath_f1_row_reused', f1RowBefore === tabsEl.querySelector('.folder-tab[data-folder-id="f1"]'));
check('fastpath_active_moved_to_f2',
  tabsEl.querySelector('.folder-tab[data-folder-id="f2"]').classList.contains('active') &&
  !tabsEl.querySelector('.folder-tab[data-folder-id="f1"]').classList.contains('active'));

// ══ Scenario D: DRAG a conversation onto a project row ══
_setFolderCalls = [];
// The source's _dragConvId is a module-scoped `let` (not reachable via
// `global`), so feed the convId the way a real drop does: via dataTransfer.
const f2row = tabsEl.querySelector('.folder-tab[data-folder-id="f2"]');
const dropEv = new window.Event('drop', { bubbles: true, cancelable: true });
Object.defineProperty(dropEv, 'target', { value: f2row });
dropEv.dataTransfer = { getData: () => 'c4' };
tabsEl.dispatchEvent(dropEv);
check('drag_assigns_folder',
  _setFolderCalls.length === 1 && _setFolderCalls[0][0] === 'c4' && _setFolderCalls[0][1] === 'f2');

// ══ Scenario E: COLLAPSE toggle persists ══
try { localStorage.removeItem('tofu_project_rail_collapsed'); } catch(e){}
sidebar.classList.remove('rail-collapsed');
_toggleProjectRail();
check('collapse_class_on', sidebar.classList.contains('rail-collapsed'));
check('collapse_persisted', localStorage.getItem('tofu_project_rail_collapsed') === '1');
_toggleProjectRail();
check('collapse_class_off', !sidebar.classList.contains('rail-collapsed'));
check('collapse_persist_cleared', localStorage.getItem('tofu_project_rail_collapsed') === '0');

// ══ Scenario F: ZERO-FOLDER degradation — no rail, no .has-rail ══
_FOLDERS_STATE = [];
_ACTIVE_FOLDER = null;
window._lastConvListHash = '';
renderConversationList();
check('zerofolder_rail_empty', tabsEl.querySelector('.folder-tab') === null);
check('zerofolder_no_has_rail', !sidebar.classList.contains('has-rail'));
// All conversations show in the single-column list (nothing filtered away).
const shownF = convList.querySelectorAll('.conv-item[data-conv-id]').length;
check('zerofolder_all_convs_shown', shownF === 4);

console.log(out.join('\n'));
"""


def _run(conv_path, folders_path):
    harness = os.path.join(HERE, '_project_rail_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, conv_path, ROOT, folders_path],
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
    'rail_has_list', 'rail_row_count', 'rail_has_uncat', 'rail_has_f1',
    'rail_has_add', 'rail_f1_count_badge', 'sidebar_has_rail_class',
    'no_legacy_pill_scroll',
    'mono_f1_two_char', 'mono_f2_initials', 'mono_len_hint',
    'color_explicit_honored', 'color_uncolored_is_hsl',
    'color_uncolored_not_accent', 'color_deterministic', 'color_distinct_keys',
    'filter_shows_only_f1', 'filter_active_row_marked',
    'fastpath_list_node_reused', 'fastpath_f1_row_reused',
    'fastpath_active_moved_to_f2',
    'drag_assigns_folder',
    'collapse_class_on', 'collapse_persisted', 'collapse_class_off',
    'collapse_persist_cleared',
    'zerofolder_rail_empty', 'zerofolder_no_has_rail',
    'zerofolder_all_convs_shown',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_project_rail_render_filter_drag():
    output = _run(_CONV, _FOLDERS)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'project-rail failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


def _nc(target, anchor, replacement, must_fail, must_still_pass):
    """Patch a COPY of `target`, run, assert target checks flip to FAIL while
    controls stay PASS, then assert the shipped file is byte-identical."""
    with open(target, encoding='utf-8') as f:
        original = f.read()
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(anchor, replacement, 1)
    assert patched != original, 'NC replacement was a no-op'
    # Suffix with THIS module's name so a sibling NC test that patches the SAME
    # shipped source (test_frontend_folder_longpress also copies
    # main_folders_mobile.js) can never collide under xdist.
    copy_path = target + '.projrail.nc_copy.js'
    conv_p, folders_p = _CONV, _FOLDERS
    if target == _CONV:
        conv_p = copy_path
    else:
        folders_p = copy_path
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(conv_p, folders_p)
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
    with open(target, encoding='utf-8') as f:
        assert f.read() == original, f'shipped {os.path.basename(target)} must be byte-identical'


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_vertical_render_is_load_bearing():
    """Neuter the rail row emission (drop the project-rail-list open tag) →
    the render checks FAIL while zero-folder degradation still PASSes."""
    _nc(
        _CONV,
        anchor="  html += '<div class=\"project-rail-list\">';",
        replacement="  html += '<div class=\"NOPE-rail-list\">';",
        must_fail=['rail_has_list'],
        must_still_pass=['zerofolder_rail_empty', 'zerofolder_no_has_rail'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_active_filter_is_load_bearing():
    """Neuter the active-folder filter (show all convs regardless) →
    filter_shows_only_f1 FAILs while the rail-render checks still PASS."""
    _nc(
        _CONV,
        anchor="      filtered = all.filter(c => c.folderId === _activeFolderId);",
        replacement="      filtered = all;",
        must_fail=['filter_shows_only_f1'],
        must_still_pass=['rail_has_list', 'rail_has_f1'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_drag_assignment_is_load_bearing():
    """Neuter the drop-handler folder assignment → drag_assigns_folder FAILs
    while the render + collapse checks still PASS."""
    _nc(
        _FOLDERS,
        anchor="    const folderId = tab.dataset.folderId || null;\n    tabsEl.querySelectorAll('.folder-tab').forEach(t => t.classList.remove('folder-tab-drop'));\n    setConversationFolder(convId, folderId);",
        replacement="    const folderId = tab.dataset.folderId || null;\n    tabsEl.querySelectorAll('.folder-tab').forEach(t => t.classList.remove('folder-tab-drop'));\n    void folderId;",
        must_fail=['drag_assigns_folder'],
        must_still_pass=['rail_has_list', 'collapse_class_on'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_monogram_is_load_bearing():
    """Neuter _folderMonogram back to a lone first-letter → the two-char
    monogram checks FAIL while the rest of the render still PASSes."""
    _nc(
        _CONV,
        anchor='    const mono = _folderMonogram(f.name);',
        replacement='    const mono = String(f.name || "").slice(0, 1).toUpperCase();',
        must_fail=['mono_f1_two_char', 'mono_f2_initials'],
        must_still_pass=['rail_has_list', 'rail_has_f1'],
    )


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_folder_color_is_load_bearing():
    """Neuter _folderColor's uncolored fallback back to the shared
    var(--accent) → the distinct/hsl checks FAIL while an explicit color and
    the render still PASS."""
    _nc(
        _CONV,
        anchor="  let h = 0;\n  for (let i = 0; i < key.length; i++) {\n    h = (h * 31 + key.charCodeAt(i)) >>> 0;\n  }\n  return `hsl(${h % 360} 52% 55%)`;",
        replacement="  void key;\n  return 'var(--accent)';",
        must_fail=['color_uncolored_is_hsl', 'color_uncolored_not_accent',
                   'color_distinct_keys'],
        must_still_pass=['color_explicit_honored', 'rail_has_list', 'rail_has_f1'],
    )


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))

"""tests/test_frontend_new_folder_top.py — New-project rail placement guard.

WHY
---
The project rail (`_renderFolderTabsInner` in
``static/js/ui/conversation_list.js``) ranks projects by the newest activity
among their MEMBER conversations. A just-created project has no members yet,
so it scored 0 and landed at the very BOTTOM of the rail — precisely when the
user wants to use it (they just made it to file the next conversation into).

The fix ranks a project by its most recent SIGNAL, treating creation itself as
a signal: ``max(newest member activity, folder.createdAt)``. A brand-new
project floats to the top (immediately under 未分类) and settles naturally as
other projects see activity.

The test drives the REAL shipped renderer under jsdom and pins BOTH sides of
that rule, because "put empty folders on top" would satisfy the headline
assertion while being wrong:

  • NEW-AT-TOP — a project created seconds ago with zero conversations is the
    first project row, directly below the 未分类 row.
  • OLD-EMPTY STAYS PUT — a long-abandoned empty project is NOT hoisted; it
    still sorts below projects with recent conversation activity.
  • ACTIVITY ORDER PRESERVED — among projects that DO have members, the
    newest-activity-first order is unchanged.
  • UNCATEGORIZED STAYS FIRST — the 未分类 row remains the top row.

Neuter (revert the comparator to member-activity-only) proves the new-at-top
assertion is load-bearing while the negative controls stay green.
Skips cleanly without node/jsdom.
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
_CONV = os.path.join(JS_DIR, 'ui', 'conversation_list.js')


def _node_deps_available() -> bool:
    if not shutil.which('node'):
        return False
    return os.path.isdir(os.path.join(ROOT, 'node_modules', 'jsdom'))


_HARNESS = r"""
const fs = require('fs');
const path = require('path');
const CONV = process.argv[2];
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));

const dom = new JSDOM(`<!DOCTYPE html><body>
  <aside class="sidebar" id="sidebar">
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

global.t = (k) => k;
global.escapeHtml = (s) => String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
global.CSS = window.CSS || { escape: (s) => s };
if (!window.CSS) window.CSS = global.CSS;

let _FOLDERS_STATE = [];
global.getFolders = () => _FOLDERS_STATE;
global.getActiveFolderId = () => null;
global.setActiveFolderId = () => {};
global.areFoldersLoaded = () => true;
global.getFolderById = (id) => _FOLDERS_STATE.find(f => f.id === id) || null;
global.setConversationFolder = () => {};
global.showToast = () => {};

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
global.isDrawerViewport = () => false;
global.isMobileViewport = () => false;
global.isTabletDrawerViewport = () => false;
global.isNearBottom = () => true;
global.scrollToBottom = () => {};
global._scheduleReflow = () => {};
global.imageGenMode = false;
global.config = {};
global.IntersectionObserver = window.IntersectionObserver =
  class { observe(){} disconnect(){} unobserve(){} };
global.setTimeout = window.setTimeout = (fn) => 0;
global.requestAnimationFrame = window.requestAnimationFrame = (fn) => 0;

(0, eval)(fs.readFileSync(CONV, 'utf8'));
global.renderConversationList = renderConversationList;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

const tabsEl = document.getElementById('folderTabs');

// Fixed clock so the scenario is deterministic.
const NOW = 1785000000000;
const DAY = 86400000;

/* Four projects covering both sides of the rule:
 *   f_new      — created 5s ago, ZERO conversations  → must be first
 *   f_oldempty — created 200 days ago, ZERO convs     → must NOT be hoisted
 *   f_act1 / f_act2 — created long ago, WITH members  → activity order kept
 * `order` is deliberately hostile to the expected result (f_new has the
 * largest order, exactly as the backend assigns it: order = len(folders)). */
_FOLDERS_STATE = [
  { id: 'f_act1',    name: 'alpha',  color: '#3b82f6', order: 0, createdAt: NOW - 300 * DAY },
  { id: 'f_oldempty',name: 'attic',  color: '#64748b', order: 1, createdAt: NOW - 200 * DAY },
  { id: 'f_act2',    name: 'bravo',  color: '#10b981', order: 2, createdAt: NOW - 100 * DAY },
  { id: 'f_new',     name: 'fresh',  color: '#f59e0b', order: 3, createdAt: NOW - 5000 },
];
global.conversations = window.conversations = [
  { id: 'c1', title: 'A', messages: [{role:'user'}], updatedAt: NOW - 30 * DAY, folderId: 'f_act1' },
  { id: 'c2', title: 'B', messages: [{role:'user'}], updatedAt: NOW - 2 * DAY,  folderId: 'f_act2' },
  { id: 'c3', title: 'C', messages: [{role:'user'}], updatedAt: NOW - DAY },   // uncategorized
];
window._lastConvListHash = '';
renderConversationList();

const rows = [...tabsEl.querySelectorAll('.project-rail-list .folder-tab[data-folder-id]')]
  .map(r => r.dataset.folderId);
out.push('# rail order: ' + JSON.stringify(rows));

// Row 0 is always the 未分类 entry (empty data-folder-id); projects follow.
check('uncat_still_first', rows[0] === '');
check('new_folder_first', rows[1] === 'f_new');
// Negative control: an OLD empty project must not ride the same lane up.
check('old_empty_not_hoisted', rows[1] !== 'f_oldempty' && rows.indexOf('f_oldempty') > rows.indexOf('f_act2'));
// Among projects WITH members, newest activity still wins (f_act2 2d > f_act1 30d).
check('activity_order_preserved', rows.indexOf('f_act2') < rows.indexOf('f_act1'));
check('all_projects_rendered', rows.length === 5);

/* Once the new project HAS a conversation, it keeps its place by activity —
 * i.e. the createdAt floor never fights the normal ranking. */
window.conversations.push(
  { id: 'c4', title: 'D', messages: [{role:'user'}], updatedAt: NOW - 10 * DAY, folderId: 'f_new' });
window._lastConvListHash = '';
renderConversationList();
const rows2 = [...tabsEl.querySelectorAll('.project-rail-list .folder-tab[data-folder-id]')]
  .map(r => r.dataset.folderId);
out.push('# rail order (populated): ' + JSON.stringify(rows2));
check('populated_new_still_top', rows2[1] === 'f_new');

console.log(out.join('\n'));
"""


def _run(conv_path):
    harness = os.path.join(HERE, '_new_folder_top_harness.js')
    with open(harness, 'w', encoding='utf-8') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, conv_path, ROOT],
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
    'uncat_still_first', 'new_folder_first', 'old_empty_not_hoisted',
    'activity_order_preserved', 'all_projects_rendered',
    'populated_new_still_top',
)


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_new_project_sorts_to_rail_top():
    output = _run(_CONV)
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'new-project rail placement failures:\n' + output
    for must in _EXPECTED:
        assert ('PASS ' + must) in output, output


@pytest.mark.skipif(not _node_deps_available(),
                    reason='node + jsdom dev-deps not installed (run npm install)')
def test_NC_createdAt_floor_is_load_bearing():
    """Revert the sort key to member-activity-only → the new project drops to
    the bottom (new_folder_first FAILs) while every negative control — which a
    naive "empty folders on top" fix would break — stays green."""
    with open(_CONV, encoding='utf-8') as f:
        original = f.read()
    anchor = ('  const _folderSortTs = (f) => '
              'Math.max(lastActiveMap[f.id] || 0, f.createdAt || 0);')
    assert anchor in original, f'NC anchor not found: {anchor[:70]!r}'
    patched = original.replace(
        anchor, '  const _folderSortTs = (f) => (lastActiveMap[f.id] || 0);', 1)
    assert patched != original, 'NC replacement was a no-op'
    copy_path = _CONV + '.newfoldertop.nc_copy.js'
    try:
        with open(copy_path, 'w', encoding='utf-8') as f:
            f.write(patched)
        output = _run(copy_path)
        assert 'FAIL new_folder_first' in output, \
            f'NC: expected new_folder_first to FAIL:\n{output}'
        for still in ('uncat_still_first', 'old_empty_not_hoisted',
                      'activity_order_preserved', 'all_projects_rendered'):
            assert ('PASS ' + still) in output, \
                f'NC must be surgical — {still} should still PASS:\n{output}'
    finally:
        try:
            os.remove(copy_path)
        except OSError:
            pass
    with open(_CONV, encoding='utf-8') as f:
        assert f.read() == original, 'shipped conversation_list.js must be byte-identical'


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))

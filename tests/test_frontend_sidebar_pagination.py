"""C3/C4 — sidebar global keyset pagination + "N earlier not loaded" (jsdom).

WHY
---
The top-N sidebar window (Epic D4, default 500) is a performance floor, not a
ceiling: conversations that sort past it must stay REACHABLE, not silently
dropped. Two pieces close that gap:

  • C3 — reaching the bottom of what's in memory triggers a keyset page fetch
    (``Api.conversations.listPage(before, beforeId, limit)``) whose rows are
    incrementally merged via ``mergeServerConvShells`` and re-rendered.
  • C4 — while "loaded < server total" a "N earlier conversations not loaded ·
    Load more" affordance is rendered at the list bottom; clicking it triggers
    the same page fetch; it disappears once caught up.

This drives the REAL shipped ``loadMoreGlobalConvs`` / ``_appendLoadMoreAffordance``
/ ``_hasMoreGlobalConvs`` / ``_unloadedGlobalConvCount`` (ui/conversation_list.js)
under node, with a stubbed DOM + a fake ``Api.conversations.listPage`` that
serves a second page of older convs. Asserts:

  1. with total(9) > loaded(3), the affordance renders with n=6;
  2. triggering load issues listPage with the OLDEST in-memory cursor;
  3. the fetched older page is merged into ``conversations`` (append, no dup);
  4. once every conv is loaded (loaded == total) the affordance is gone.

NEUTER: stub listPage to return nothing and prove the affordance persists /
convs never grow — the fetch is load-bearing.

Runs the REAL shipped JS under node; skips cleanly when node isn't installed.
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

// ── Minimal DOM: a list element that records appended children. ──
function makeEl(tag) {
  return {
    tagName: tag, className: '', textContent: '', type: '',
    children: [], _listeners: {},
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(ev, fn) { this._listeners[ev] = fn; },
    click() { if (this._listeners.click) this._listeners.click(); },
  };
}
const listEl = makeEl('div');
// Count renderConversationList invocations by spying on its convList lookup —
// the file's OWN top-level renderConversationList declaration shadows any JS
// stub (direct-eval scope binding), but it always starts by resolving
// document.getElementById('convList'), which we CAN observe here.
let renderLookups = 0;
global.document = {
  createElement: (t) => makeEl(t),
  getElementById: (id) => {
    if (id === 'convList') { renderLookups++; return null; }
    return null;
  },
};

// t(): identity-ish translator that fills {n}.
global.t = (key) => {
  if (key === 'sidebar.loadMoreEarlier') return '{n} earlier conversations not loaded · Load more';
  return key;
};

// Folder view is OFF (global list).
global.getActiveFolderId = () => null;

// In-memory sidebar window: only the 3 NEWEST convs (server has 9 total).
global.conversations = [
  { id: 'c9', title: 'c9', updatedAt: 9000, createdAt: 9000, messages: [], _serverMsgCount: 1 },
  { id: 'c8', title: 'c8', updatedAt: 8000, createdAt: 8000, messages: [], _serverMsgCount: 1 },
  { id: 'c7', title: 'c7', updatedAt: 7000, createdAt: 7000, messages: [], _serverMsgCount: 1 },
];

// Server reports 9 total.
let _total = 9;
global.getServerTotalCount = () => _total;

global.renderConversationList = () => {};

// A minimal real mergeServerConvShells (id-keyed, append shells, never dup).
global._serverConvCount = (sc) => (sc && (sc.messageCount != null ? sc.messageCount
  : (sc.msgCount != null ? sc.msgCount : sc.msg_count))) || 0;
global._applySettingsToConv = () => {};
global.mergeServerConvShells = (rows) => {
  if (!Array.isArray(rows)) return 0;
  const map = new Map(conversations.map(c => [c.id, c]));
  let added = 0;
  for (const sc of rows) {
    if (!sc || !sc.id) continue;
    if (map.has(sc.id)) continue;
    const cnt = _serverConvCount(sc);
    conversations.push({ id: sc.id, title: sc.title, messages: [],
      _serverMsgCount: cnt, _needsLoad: cnt > 0,
      createdAt: sc.createdAt, updatedAt: sc.updatedAt || sc.createdAt });
    added++;
  }
  return added;
};

// Fake listPage: serves convs strictly OLDER than the given cursor.
let listPageArgs = null;
const ALL_OLDER = [
  { id: 'c6', title: 'c6', messageCount: 1, createdAt: 6000, updatedAt: 6000 },
  { id: 'c5', title: 'c5', messageCount: 1, createdAt: 5000, updatedAt: 5000 },
  { id: 'c4', title: 'c4', messageCount: 1, createdAt: 4000, updatedAt: 4000 },
  { id: 'c3', title: 'c3', messageCount: 1, createdAt: 3000, updatedAt: 3000 },
  { id: 'c2', title: 'c2', messageCount: 1, createdAt: 2000, updatedAt: 2000 },
  { id: 'c1', title: 'c1', messageCount: 1, createdAt: 1000, updatedAt: 1000 },
];
const NEUTER = process.argv[3] === 'neuter';
global.Api = {
  conversations: {
    listPage: async (before, beforeId, limit) => {
      listPageArgs = { before, beforeId, limit };
      if (NEUTER) return { conversations: [], hasMore: false };
      const older = ALL_OLDER.filter(c => c.updatedAt < before).slice(0, limit || 100);
      return { conversations: older, hasMore: older.length >= (limit || 100) };
    },
  },
};

// Stub the heavy render helpers conversation_list.js references so the file
// loads; the pager fns we exercise don't call them.
global.escapeHtml = (s) => s;
global.stripNoTranslateTags = (s) => s;
global._buildConvItemHTML = () => '';
global.activeConvId = null;

// Load the REAL shipped conversation_list.js (top-level fn declarations).
eval(fs.readFileSync(process.argv[2], 'utf8'));



const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  check('fn_loadMoreGlobalConvs', typeof loadMoreGlobalConvs === 'function');
  check('fn_appendLoadMoreAffordance', typeof _appendLoadMoreAffordance === 'function');

  // (1) affordance shows n=6 when loaded(3) < total(9).
  check('hasMore_true_initially', _hasMoreGlobalConvs() === true);
  check('unloaded_count_6', _unloadedGlobalConvCount() === 6);
  _appendLoadMoreAffordance(listEl);
  const btn = listEl.children[listEl.children.length - 1];
  check('affordance_rendered', !!btn && btn.className === 'conv-load-more');
  check('affordance_label_has_6', !!btn && /\b6\b/.test(btn.textContent));

  // (2) triggering load issues listPage with the OLDEST cursor (c7 @ 7000).
  await loadMoreGlobalConvs();
  for (let k = 0; k < 50; k++) await Promise.resolve();
  check('listPage_issued', !!listPageArgs);
  check('cursor_is_oldest_updatedAt', listPageArgs && listPageArgs.before === 7000);
  check('cursor_is_oldest_id', listPageArgs && listPageArgs.beforeId === 'c7');

  // (3) older page merged in (append, no dup).
  const ids = conversations.map(c => c.id);
  check('older_page_merged_c6', ids.includes('c6'));
  check('older_page_merged_c1', ids.includes('c1'));
  const uniq = new Set(ids);
  check('no_duplicate_ids', uniq.size === ids.length);
  check('rerender_fired', renderLookups > 0);

  // (4) now loaded == total(9): affordance no longer applies.
  check('all_9_loaded', conversations.length === 9);
  check('hasMore_false_after', _hasMoreGlobalConvs() === false);
  check('unloaded_count_0', _unloadedGlobalConvCount() === 0);
  const list2 = makeEl('div');
  _appendLoadMoreAffordance(list2);
  check('affordance_gone_when_caught_up', list2.children.length === 0);

  console.log(out.join('\n'));
})();
"""


def _run(neuter=False):
    harness = os.path.join(HERE, '_sidebar_pager_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    conv_list_js = os.path.join(JS_DIR, 'ui', 'conversation_list.js')
    argv = ['node', harness, conv_list_js]
    if neuter:
        argv.append('neuter')
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=60)
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_sidebar_global_pagination_and_affordance():
    proc = _run()
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'C3/C4 sidebar pagination regression:\n' + output
    for must in ('PASS unloaded_count_6',
                 'PASS listPage_issued',
                 'PASS cursor_is_oldest_updatedAt',
                 'PASS older_page_merged_c1',
                 'PASS no_duplicate_ids',
                 'PASS affordance_gone_when_caught_up'):
        assert must in output, f'missing assertion {must!r}:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_NC_without_page_fetch_convs_never_grow():
    """NEUTER: listPage returns nothing → convs never grow and the affordance
    persists — proving the keyset fetch is load-bearing."""
    proc = _run(neuter=True)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed (neuter): {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('all_9_loaded') is False, (
        'NEUTER did not bite: convs grew even though listPage served nothing.\n' + output)
    assert lines.get('affordance_gone_when_caught_up') is False, (
        'NEUTER did not bite: affordance vanished without any page being loaded.\n' + output)

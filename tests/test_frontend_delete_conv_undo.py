"""Regression test: deleting a conversation is recoverable via the undo toast,
AND the load-bearing case — an UNLOADED sidebar shell (``_needsLoad:true``,
``messages:[]`` in memory, history only on the server) — restores its FULL
history on undo.

WHY
---
The first cut of the undo feature snapshotted the conv object as-is. For the
single most common deletion target — a conv loaded this session as a shell
(``_needsLoad:true`` / ``messages:[]``) — the snapshot captured ZERO messages,
so on undo ``_restoreDeletedConversation`` skipped the server re-create
(``syncConversationToServer`` requires ``messages.length > 0``) and the entire
conversation was permanently lost despite the toast claiming "restored".

The first fix made ``deleteConversation`` async: hydrate via
``loadConversationMessages(id)`` BEFORE snapshotting. Restore must also
preserve the original ``updatedAt`` (no sidebar re-stamp to load-time).

INSTANT-UI CONTRACT (owner directive 2026-07-31, epic pt_0b444c0be11a4048)
--------------------------------------------------------------------------
The hydrate-first fix made the CLICK itself blocking: on a shell/windowed conv
over a slow tunnel, delete showed no visible effect for seconds (and could
stack a "delete without undo?" consent dialog on top), inviting repeated
clicks. The contract is now **optimistic delete**:

1. In the SAME task as the click (zero awaits): kill live streams/tasks,
   snapshot what we hold, remove the conv from the sidebar, evict the cache,
   broadcast, switch/re-render.
2. In the BACKGROUND: hydrate the snapshot via ONE full ``window=0`` GET (the
   server row still exists — the DELETE only fires AFTER this await, so the
   GET cannot 404), then fire-and-forget the DELETE, then show the undo toast
   (or a plain "deleted" toast when hydration failed and the snapshot is
   hollow — fail-open WITHOUT the retired blocking consent dialog).

This drives the REAL shipped ``deleteConversation`` /
``_restoreDeletedConversation`` under node, stubbing the network seam
(``Api.conversations.get/put/remove``). Skips cleanly when node isn't
installed.
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


def _node_available() -> bool:
    return bool(shutil.which('node'))


# Harness: define every global deleteConversation/_restore touch, then eval the
# REAL main_conv_lifecycle.js (which is all top-level declarations) so the
# shipped functions are defined against our stubs. We then exercise the
# delete→undo round-trip for an unloaded shell conv and assert the server PUT
# carries the full history with the original updatedAt — PLUS the instant-UI
# pins: the conv must be gone from the list SYNCHRONOUSLY (before the first
# network call), and hydration must ride the full window=0 GET ordered BEFORE
# the DELETE.
_HARNESS = r"""
const fs = require('fs');
global.window = global;

// ── In-memory state the functions mutate ──
const ORIGINAL_MSGS = [
  { role: 'user', content: 'hello', timestamp: 100 },
  { role: 'assistant', content: 'hi there', timestamp: 200 },
];
const ORIGINAL_UPDATED_AT = 1700000000000;  // a fixed past timestamp

// The shell conv as it sits in the sidebar: loaded as metadata only.
let shell = {
  id: 'conv-shell-1',
  title: 'Important chat',
  messages: [],                 // ← EMPTY in memory (the trap)
  _needsLoad: true,             // ← history lives only on the server
  _serverMsgCount: 2,
  createdAt: 1699999000000,
  updatedAt: ORIGINAL_UPDATED_AT,
};
let otherConv = { id: 'conv-other', title: 'Other', messages: [{role:'user',content:'x',timestamp:1}], updatedAt: 1699990000000 };
global.conversations = [shell, otherConv];
// Keep the shell NON-active: the data-loss bug is identical whether or not the
// deleted conv is active, and a non-active delete avoids driving the real
// loadConversation() (a top-level `function` decl that shadows any stub and
// pulls in dozens of unrelated globals). wasActive=false is the path under test.
global.activeConvId = 'conv-other';

// ── Records of network calls (+ an ordering log) ──
const calls = { loadMsgs: [], fullGet: [], put: [], remove: [], abortTask: [], seq: [] };

// loadConversationMessages is the VIEWING path — the optimistic delete must
// NOT route hydration through it (the conv is already detached from the list).
global.loadConversationMessages = async function(id) {
  calls.loadMsgs.push(id);
  const c = conversations.find(x => x.id === id);
  if (!c) return null;
  c.messages = JSON.parse(JSON.stringify(ORIGINAL_MSGS));
  c._needsLoad = false;
  c._serverMsgCount = ORIGINAL_MSGS.length;
  return c;
};

// syncConversationToServer is the REAL restore re-create path? No — it's a
// huge function with many deps. Stub it to capture what restore would ship,
// mirroring its real contract: PUT with messages + updatedAt||Date.now().
global.syncConversationToServer = async function(conv) {
  if (!conv.messages || conv.messages.length === 0) return;  // real guard
  calls.put.push({
    id: conv.id,
    msgCount: conv.messages.length,
    messages: JSON.parse(JSON.stringify(conv.messages)),
    updatedAt: conv.updatedAt || Date.now(),
  });
};

global.Api = {
  conversations: {
    // The full (window=0) GET the background hydration rides.
    get: async (id, opts) => {
      calls.fullGet.push({ id, window: opts && opts.query && opts.query.window });
      calls.seq.push('get');
      return { id, title: 'Important chat', messages: JSON.parse(JSON.stringify(ORIGINAL_MSGS)), rev: 3 };
    },
    remove: async (id) => { calls.remove.push(id); calls.seq.push('remove'); return { ok: true }; },
  },
  chat: { abortTask: async (tid) => { calls.abortTask.push(tid); return { ok: true }; } },
};
global.ConvCache = { remove() { calls._cacheRemoved = true; calls.seq.push('cache'); }, put() {} };
global.activeStreams = new Map();
global._broadcastToTabs = function(type) { calls._broadcast = type; calls.seq.push('broadcast'); };
global.renderConversationList = function() { calls.seq.push('render'); };
global.newChat = function() { global.activeConvId = null; };
global.loadConversation = function(id) { global.activeConvId = id; };
global.debugLog = function() {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
// showToast just records that restore announced success.
const toasts = [];
global.showToast = (...a) => toasts.push(a);

// DOM stub: a toast container that captures the undo button so the test can
// "click" it. The undo toast appends one element with a .toast-undo-btn.
let _undoHandler = null;
const _container = {
  appendChild(el) { _container._last = el; },
};
global.document = {
  getElementById(id) { return id === 'toastContainer' ? _container : null; },
  createElement() {
    const el = {
      className: '', _html: '', _listeners: {},
      set innerHTML(v) { this._html = v; },
      get innerHTML() { return this._html; },
      classList: { add() {} },
      remove() {},
      querySelector(sel) {
        // return a fake node whose addEventListener('click', fn) captures fn
        if (sel === '.toast-undo-btn') {
          return { addEventListener(ev, fn) { if (ev === 'click') _undoHandler = fn; } };
        }
        return { style: {} };
      },
      addEventListener() {},
    };
    return el;
  },
};
global.setTimeout = (fn) => 0;     // never auto-dismiss during the test
global.clearTimeout = () => {};

// ── Load the REAL shipped functions ──
eval(fs.readFileSync(process.argv[2], 'utf8'));  // main_conv_lifecycle.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof deleteConversation !== 'function') { console.log('FAIL fn_exposed deleteConversation missing'); return; }
  if (typeof _restoreDeletedConversation !== 'function') { console.log('FAIL fn_exposed _restoreDeletedConversation missing'); return; }
  check('fns_exposed', true);

  // ── DELETE the shell conv (do NOT await yet — the instant-UI pins below
  //    must observe the state after the SYNC prefix but BEFORE the first
  //    network await resolves). ──
  const p = deleteConversation('conv-shell-1');

  // ★ INSTANT-UI: the conv is gone from the list, cache evicted, broadcast
  //   sent — all SYNCHRONOUSLY, before any network call was even initiated.
  //   (On the old hydrate-first code every one of these FAILS: the function
  //   awaited loadConversationMessages before touching the list.)
  check('removed_instantly', !conversations.some(c => c.id === 'conv-shell-1'));
  check('cache_evicted_instantly', !!calls._cacheRemoved);
  check('deleted_broadcast_instantly', calls._broadcast === 'conv_deleted');
  // …and every UI side-effect was ordered BEFORE the first network call was
  //   even initiated (the hydrate GET's stub runs synchronously, so a
  //   network-first regression is visible in the ordering log).
  const _firstNet = calls.seq.findIndex(x => x === 'get' || x === 'remove');
  check('ui_ordered_before_network', _firstNet > 0
        && ['cache', 'broadcast', 'render'].every(x => calls.seq.indexOf(x) !== -1 && calls.seq.indexOf(x) < _firstNet));

  await p;

  // 1. Background hydration rode ONE full (window=0) GET, ordered BEFORE the
  //    server DELETE (else the GET would 404).
  check('hydrate_via_full_get', calls.fullGet.length === 1 && calls.fullGet[0].window === '0');
  check('hydrate_ordered_before_delete',
        calls.seq.filter(x => x === 'get' || x === 'remove').join(',') === 'get,remove');
  check('no_load_msgs_route', calls.loadMsgs.length === 0);
  // 2. Server DELETE fired.
  check('server_delete_fired', calls.remove.length === 1 && calls.remove[0] === 'conv-shell-1');
  // 3. Conv still removed from the in-memory list.
  check('removed_from_memory', !conversations.some(c => c.id === 'conv-shell-1'));
  // 4. The undo handler was registered (toast shown with an Undo button).
  check('undo_button_registered', typeof _undoHandler === 'function');

  // ── UNDO: click the Undo button ──
  _undoHandler();

  // 5. Conv is back in memory…
  const back = conversations.find(c => c.id === 'conv-shell-1');
  check('restored_in_memory', !!back);
  // 6. …with its FULL history (this is the bug — it used to be empty).
  check('restored_full_history', !!back && back.messages.length === ORIGINAL_MSGS.length);
  check('restored_first_msg', !!back && back.messages[0] && back.messages[0].content === 'hello');
  // 7. The server row was RE-CREATED via PUT, carrying the full messages.
  const lastPut = calls.put[calls.put.length - 1];
  check('server_recreate_put', !!lastPut && lastPut.id === 'conv-shell-1');
  check('recreate_put_full_msgs', !!lastPut && lastPut.msgCount === ORIGINAL_MSGS.length);
  // 8. updatedAt preserved (NOT re-stamped to Date.now()).
  check('updatedAt_preserved', !!lastPut && lastPut.updatedAt === ORIGINAL_UPDATED_AT);
  check('restored_updatedAt_in_mem', !!back && back.updatedAt === ORIGINAL_UPDATED_AT);
  // 9. Restored at its original sidebar index (0).
  check('restored_at_orig_index', conversations[0] && conversations[0].id === 'conv-shell-1');
  // 10. Shell flag cleared on the restored conv.
  check('needsLoad_cleared', !!back && !back._needsLoad);

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_conv_undo_restores_unloaded_shell():
    harness = os.path.join(HERE, '_delete_conv_undo_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'main', 'main_conv_lifecycle.js'),  # argv[2]
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
    assert not fails, 'delete→undo round-trip failures:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'


# ── Hydration-failure = fail-OPEN, instantly, WITHOUT the retired consent
#    dialog. When the server can't be reached at delete time, the background
#    window=0 GET rejects, so the undo snapshot stays hollow. Historically the
#    delete was HARD-REFUSED here, then became ask-consent-then-delete — both
#    BLOCKED the click on network conditions (the reported "delete always
#    fails" / "no response for seconds" bugs). The instant-UI contract deletes
#    immediately and simply skips the (hollow) undo toast. ──
_HARNESS_FAIL = r"""
const fs = require('fs');
global.window = global;

let shell = {
  id: 'conv-shell-1',
  title: 'Important chat',
  messages: [],            // ← EMPTY, and stays empty (hydration fails)
  _needsLoad: true,
  _serverMsgCount: 2,      // ← but the SERVER has 2 messages
  createdAt: 1699999000000,
  updatedAt: 1700000000000,
};
let otherConv = { id: 'conv-other', title: 'Other', messages: [{role:'user',content:'x',timestamp:1}], updatedAt: 1699990000000 };
global.conversations = [shell, otherConv];
global.activeConvId = 'conv-other';

const calls = { loadMsgs: [], fullGet: [], put: [], remove: [], abortTask: [], confirm: 0, seq: [] };

// Legacy viewing-path hydration — must NOT be called by the optimistic flow.
global.loadConversationMessages = async function(id) {
  calls.loadMsgs.push(id);
  return null;
};
global.syncConversationToServer = async function(conv) {
  if (!conv.messages || conv.messages.length === 0) return;
  calls.put.push({ id: conv.id, msgCount: conv.messages.length });
};
global.Api = {
  conversations: {
    // The background hydration GET REJECTS — server unreachable.
    get: async (id) => { calls.fullGet.push(id); calls.seq.push('get'); throw new Error('server unreachable'); },
    remove: async (id) => { calls.remove.push(id); calls.seq.push('remove'); return { ok: true }; },
  },
  chat: { abortTask: async (tid) => { calls.abortTask.push(tid); return { ok: true }; } },
};
global.ConvCache = { remove() { calls._cacheRemoved = true; }, put() {} };
global.activeStreams = new Map();
global._broadcastToTabs = function(type) { calls._broadcast = type; };
global.renderConversationList = function() {};
global.newChat = function() { global.activeConvId = null; };
global.loadConversation = function(id) { global.activeConvId = id; };
global.debugLog = function() {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
const toasts = [];
global.showToast = (...a) => toasts.push(a);
// The retired consent dialog — the instant-UI flow must NEVER call it.
global.showConfirm = async function() { calls.confirm++; return false; };

// DOM stub: capture whether an undo button was registered (it must NOT be
// for the delete-without-undo path).
let _undoHandler = null;
const _container = { appendChild() {} };
global.document = {
  getElementById(id) { return id === 'toastContainer' ? _container : null; },
  createElement() {
    return {
      className: '', set innerHTML(v) {}, get innerHTML() { return ''; },
      classList: { add() {} }, remove() {},
      querySelector(sel) {
        if (sel === '.toast-undo-btn') return { addEventListener(ev, fn) { if (ev === 'click') _undoHandler = fn; } };
        return { style: {} };
      },
      addEventListener() {},
    };
  },
};
global.setTimeout = (fn) => 0;
global.clearTimeout = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // main_conv_lifecycle.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof deleteConversation !== 'function') { console.log('FAIL fn_exposed deleteConversation missing'); return; }
  check('fn_exposed', true);

  const p = deleteConversation('conv-shell-1');

  // ★ INSTANT-UI: the delete MUST NOT wait on the network OR the user's
  //   consent. (On the old code these FAIL — it awaited hydration first and
  //   then BLOCKED on showConfirm.)
  check('removed_instantly', !conversations.some(c => c.id === 'conv-shell-1'));
  check('cache_evicted_instantly', !!calls._cacheRemoved);
  check('deleted_broadcast_instantly', calls._broadcast === 'conv_deleted');
  check('no_consent_gate', calls.confirm === 0);

  await p;

  // The background hydration was attempted (and failed), then the DELETE
  // still fired — fail-open, the server row is authoritative.
  check('hydrate_attempted', calls.fullGet.length === 1);
  check('server_delete_fired', calls.remove.length === 1 && calls.remove[0] === 'conv-shell-1');
  check('hydrate_ordered_before_delete', calls.seq.join(',') === 'get,remove');
  check('conv_stays_removed', !conversations.some(c => c.id === 'conv-shell-1'));
  // No FALSE undo: the snapshot is hollow, so NO undo button is registered…
  check('no_undo_handler', _undoHandler === null);
  // …and the user sees a plain "deleted" toast (not a restorable one).
  check('plain_deleted_toast', toasts.some(a => a[0] === 'sidebar.convDeleted'));

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_conv_hydration_fail_deletes_without_undo():
    """Hydration fails → the delete STILL lands instantly (no consent gate,
    no blocking), the background DELETE fires, and only the (hollow) undo
    affordance is skipped in favour of a plain "deleted" toast."""
    harness = os.path.join(HERE, '_delete_conv_fail_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS_FAIL)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'main', 'main_conv_lifecycle.js'),  # argv[2]
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
    assert not fails, 'delete hydration-fail failures:\n' + output
    assert output.count('PASS') >= 10, f'expected >=10 PASS lines, got:\n{output}'


# ── HAPPY PATH regression guard (the MOST COMMON delete): an already-loaded,
#    NON-shell conv (messages populated, _needsLoad falsy, messages.length >=
#    _serverMsgCount). It must fire the DELETE with ZERO hydration and a NORMAL
#    undo toast — i.e. the _needsHydrate gate must NOT engage.
#
# ── WINDOWED-TAIL completeness (data-loss fix, point #2): a conv opened with a
#    windowed read holds only the tail (messages.length < _serverMsgCount) with
#    _windowed=true. Snapshotting that would let undo re-create a conv missing
#    its oldest messages. The background hydration must FORCE a full (window=0)
#    fetch to complete the body BEFORE the DELETE fires, then offer a REAL undo
#    carrying every message. ──
_HARNESS_COMPLETE = r"""
const fs = require('fs');
global.window = global;

// argv[3]: 'loaded'   — non-shell, fully-loaded conv (happy path, point #1)
//          'windowed' — windowed tail (60 of 74) that MUST complete via window=0
const scenario = process.argv[3] || 'loaded';

// Full server history the FULL (window=0) fetch returns (74 msgs).
const FULL_MSGS = Array.from({ length: 74 }, (_, i) => ({
  role: i % 2 === 0 ? 'user' : 'assistant', content: 'm' + i, timestamp: 1000 + i,
}));
const TAIL = FULL_MSGS.slice(14);   // the windowed open loaded only the tail 60

let conv;
if (scenario === 'loaded') {
  // Already fully loaded in memory — NOT a shell, holds every server message.
  conv = {
    id: 'conv-1', title: 'Loaded chat',
    messages: JSON.parse(JSON.stringify(FULL_MSGS)),
    _needsLoad: false, _serverMsgCount: 74,
    createdAt: 1, updatedAt: 1700000000000,
  };
} else {
  // Windowed open: only the tail is in memory, but _serverMsgCount is the TOTAL.
  conv = {
    id: 'conv-1', title: 'Windowed chat',
    messages: JSON.parse(JSON.stringify(TAIL)),   // 60 of 74
    _needsLoad: false, _serverMsgCount: 74,
    _windowed: true, _hasMoreEarlier: true, _trimmed: true,
    createdAt: 1, updatedAt: 1700000000000,
  };
}
let otherConv = { id: 'conv-other', title: 'Other', messages: [{role:'user',content:'x',timestamp:1}], updatedAt: 1 };
global.conversations = [conv, otherConv];
global.activeConvId = 'conv-other';   // non-active delete (see shell harness note)

const calls = { loadMsgs: [], fullGet: [], put: [], remove: [], confirm: 0, seq: [] };

// loadConversationMessages: the VIEWING path — must not be used by delete.
global.loadConversationMessages = async function(id) {
  calls.loadMsgs.push(id);
  return conversations.find(x => x.id === id) || null;
};
// Api.conversations.get with {query:{window:'0'}} → the FULL untrimmed array.
global.Api = {
  conversations: {
    get: async (id, opts) => {
      calls.fullGet.push({ id, window: opts && opts.query && opts.query.window });
      calls.seq.push('get');
      return { id, title: 'Windowed chat', messages: JSON.parse(JSON.stringify(FULL_MSGS)), rev: 5 };
    },
    remove: async (id) => { calls.remove.push(id); calls.seq.push('remove'); return { ok: true }; },
  },
  chat: { abortTask: async () => ({ ok: true }) },
};
global.syncConversationToServer = async function(c) {
  if (!c.messages || c.messages.length === 0) return;
  calls.put.push({ id: c.id, msgCount: c.messages.length, first: c.messages[0] && c.messages[0].content });
};
global.ConvCache = { remove() { calls._cacheRemoved = true; }, put() {} };
global.activeStreams = new Map();
global._broadcastToTabs = function(type) { calls._broadcast = type; };
global.renderConversationList = function() {};
global.newChat = function() {};
global.loadConversation = function() {};
global.debugLog = function() {};
global.escapeHtml = (s) => String(s == null ? '' : s);
global.t = (k) => k;
global.showToast = () => {};
global.showConfirm = async function() { calls.confirm++; return true; };  // must NOT be called

let _undoHandler = null;
const _container = { appendChild() {} };
global.document = {
  getElementById(id) { return id === 'toastContainer' ? _container : null; },
  createElement() {
    return {
      className: '', set innerHTML(v) {}, get innerHTML() { return ''; },
      classList: { add() {} }, remove() {},
      querySelector(sel) {
        if (sel === '.toast-undo-btn') return { addEventListener(ev, fn) { if (ev === 'click') _undoHandler = fn; } };
        return { style: {} };
      },
      addEventListener() {},
    };
  },
};
global.setTimeout = (fn) => 0;
global.clearTimeout = () => {};

eval(fs.readFileSync(process.argv[2], 'utf8'));  // main_conv_lifecycle.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

(async () => {
  if (typeof deleteConversation !== 'function') { console.log('FAIL fn_exposed'); return; }
  check('fn_exposed', true);

  const p = deleteConversation('conv-1');

  // ★ INSTANT-UI: removal + cache-evict + broadcast are SYNCHRONOUS.
  check('removed_instantly', !conversations.some(c => c.id === 'conv-1'));
  check('cache_evicted_instantly', !!calls._cacheRemoved);
  check('deleted_broadcast_instantly', calls._broadcast === 'conv_deleted');

  await p;

  // In BOTH scenarios the delete goes through with a REAL undo and no consent.
  check('no_consent_prompt', calls.confirm === 0);
  check('server_delete_fired', calls.remove.length === 1 && calls.remove[0] === 'conv-1');
  check('conv_removed', !conversations.some(c => c.id === 'conv-1'));
  check('undo_registered', typeof _undoHandler === 'function');
  check('no_load_msgs_route', calls.loadMsgs.length === 0);

  if (scenario === 'loaded') {
    // Happy path: no hydrate at all — the body was already complete.
    check('no_full_get', calls.fullGet.length === 0);
  } else {
    // Windowed: it must have FORCED a full (window='0') fetch, ordered BEFORE
    // the DELETE, to complete the snapshot.
    check('full_get_forced', calls.fullGet.length === 1 && calls.fullGet[0].window === '0');
    check('hydrate_ordered_before_delete', calls.seq.join(',') === 'get,remove');
  }

  // ── UNDO: the snapshot must carry the COMPLETE 74-msg history (incl. head). ──
  _undoHandler();
  const back = conversations.find(c => c.id === 'conv-1');
  check('restored_in_memory', !!back);
  check('restored_full_74', !!back && back.messages.length === 74);
  check('restored_head_present', !!back && back.messages[0] && back.messages[0].content === 'm0');
  const lastPut = calls.put[calls.put.length - 1];
  check('recreate_put_full_74', !!lastPut && lastPut.msgCount === 74);
  check('recreate_put_head', !!lastPut && lastPut.first === 'm0');

  console.log(out.join('\n'));
})();
"""


def _run_complete_harness(scenario: str) -> str:
    harness = os.path.join(HERE, f'_delete_conv_complete_harness_{scenario}.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS_COMPLETE)
    try:
        proc = subprocess.run(
            ['node', harness,
             os.path.join(JS_DIR, 'main', 'main_conv_lifecycle.js'),  # argv[2]
             scenario,                                                 # argv[3]
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
    assert not fails, f'delete completeness ({scenario}) failures:\n' + output
    return output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_conv_loaded_nonshell_fires_delete_with_undo():
    """HAPPY PATH regression guard (the most common case): an already-loaded
    non-shell conv deletes instantly with NO hydrate, NO showConfirm, and a
    normal undo toast — the _needsHydrate gate must not engage."""
    output = _run_complete_harness('loaded')
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_conv_windowed_tail_completes_body_before_snapshot():
    """DATA-LOSS FIX (point #2): a windowed-tail conv (60 of 74 in memory,
    _serverMsgCount=74) must FORCE a full window=0 fetch in the background
    (ordered BEFORE the DELETE) so undo restores the COMPLETE 74-msg history —
    not a head-truncated conv."""
    output = _run_complete_harness('windowed')
    assert output.count('PASS') >= 11, f'expected >=11 PASS lines, got:\n{output}'

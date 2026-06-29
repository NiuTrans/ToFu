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

The fix makes ``deleteConversation`` async: it hydrates a shell conv via
``loadConversationMessages(id)`` BEFORE snapshotting, so the snapshot carries
the full body and undo re-creates the row with messages intact. Restore must
also preserve the original ``updatedAt`` (no sidebar re-stamp to load-time).

This drives the REAL shipped ``deleteConversation`` /
``_restoreDeletedConversation`` under node, stubbing the network seam
(``Api.conversations.put/remove``) + ``loadConversationMessages`` (which
mimics the server fetch by populating ``conv.messages``). Skips cleanly when
node isn't installed.
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


# Harness: define every global deleteConversation/_restore touch, then eval the
# REAL main_conv_lifecycle.js (which is all top-level declarations) so the
# shipped functions are defined against our stubs. We then exercise the
# delete→undo round-trip for an unloaded shell conv and assert the server PUT
# carries the full history with the original updatedAt.
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

// ── Records of network calls ──
const calls = { loadMsgs: [], put: [], remove: [], abortTask: [] };

// loadConversationMessages: simulate the server fetch by populating messages
// in place + clearing the shell flag (exactly what the real one does).
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
  conversations: { remove: async (id) => { calls.remove.push(id); return { ok: true }; } },
  chat: { abortTask: async (tid) => { calls.abortTask.push(tid); return { ok: true }; } },
};
global.ConvCache = { remove() {}, put() {} };
global.activeStreams = new Map();
global._broadcastToTabs = function() {};
global.renderConversationList = function() {};
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

  // ── DELETE the shell conv (await — it's now async) ──
  await deleteConversation('conv-shell-1');

  // 1. It must have hydrated the shell from the server BEFORE deleting.
  check('hydrated_before_delete', calls.loadMsgs.length === 1 && calls.loadMsgs[0] === 'conv-shell-1');
  // 2. Server DELETE fired.
  check('server_delete_fired', calls.remove.length === 1 && calls.remove[0] === 'conv-shell-1');
  // 3. Conv removed from the in-memory list.
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
    assert output.count('PASS') >= 12, f'expected >=12 PASS lines, got:\n{output}'


# ── Abort-on-hydration-failure: when the server can't be reached at delete
#    time, loadConversationMessages leaves messages:[] (the shell could not be
#    materialised). Deleting now would leave the undo snapshot hollow → permanent
#    loss precisely when the network is flaky. The delete MUST be refused. ──
_HARNESS_ABORT = r"""
const fs = require('fs');
global.window = global;

let shell = {
  id: 'conv-shell-1',
  title: 'Important chat',
  messages: [],            // ← EMPTY, and stays empty (load fails)
  _needsLoad: true,
  _serverMsgCount: 2,      // ← but the SERVER has 2 messages
  createdAt: 1699999000000,
  updatedAt: 1700000000000,
};
let otherConv = { id: 'conv-other', title: 'Other', messages: [{role:'user',content:'x',timestamp:1}], updatedAt: 1699990000000 };
global.conversations = [shell, otherConv];
global.activeConvId = 'conv-other';

const calls = { loadMsgs: [], put: [], remove: [], abortTask: [] };

// loadConversationMessages FAILS to materialise the body — simulates the
// server being unreachable: messages STAYS empty, _needsLoad stays true.
global.loadConversationMessages = async function(id) {
  calls.loadMsgs.push(id);
  /* no-op: do NOT populate messages (server unreachable) */
  return null;
};
global.syncConversationToServer = async function(conv) {
  if (!conv.messages || conv.messages.length === 0) return;
  calls.put.push({ id: conv.id, msgCount: conv.messages.length });
};
global.Api = {
  conversations: { remove: async (id) => { calls.remove.push(id); return { ok: true }; } },
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

// DOM stub: capture whether an undo button was registered (it must NOT be).
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

  await deleteConversation('conv-shell-1');

  // It attempted to hydrate…
  check('hydrate_attempted', calls.loadMsgs.length === 1);
  // (a) …but the server DELETE was NOT issued.
  check('no_server_delete', calls.remove.length === 0);
  // (b) …the conv is STILL in the list.
  check('conv_still_present', conversations.some(c => c.id === 'conv-shell-1'));
  // (c) …no undo toast/handler was registered.
  check('no_undo_handler', _undoHandler === null);
  // It did NOT broadcast a deletion, nor evict the cache.
  check('no_deleted_broadcast', calls._broadcast !== 'conv_deleted');
  check('cache_not_evicted', !calls._cacheRemoved);
  // It surfaced an error toast telling the user.
  check('error_toast_shown', toasts.some(a => a[0] === 'sidebar.deleteFailed'));

  console.log(out.join('\n'));
})();
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_delete_conv_aborts_when_hydration_fails():
    harness = os.path.join(HERE, '_delete_conv_abort_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS_ABORT)
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
    assert not fails, 'abort-on-hydration-failure failures:\n' + output
    assert output.count('PASS') >= 7, f'expected >=7 PASS lines, got:\n{output}'

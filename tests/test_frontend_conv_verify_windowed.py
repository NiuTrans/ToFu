"""Guard: _verifyActiveConvFromServer reads the TAIL WINDOW, not the full blob
(pt_afbaf3d7 ③ — the hottest member of the 2026-08-01 congestion-collapse
storm: this verify fired a full 176.8 MB GET on every conv_changed frame and
every push-reconnect catch-up).

The function now requests ``?window=N`` (convWindowParam) and adopts from the
windowed envelope:

  A. growth    — new messages after the local tail's _msgId ANCHOR are
                 APPENDED (local message objects keep their identity — the
                 non-destructive contract); _serverMsgCount comes from the
                 envelope's totalCount; rev advances.
  B. in-place  — the anchor PAIR gets the legacy Case-2 trailing-growth
                 adoption (content grows → server wins).
  C. escalate  — the anchor is NOT in the tail window (more new messages
                 than the window, or identity mismatch) → exactly ONE full
                 refetch (window:'0') and the legacy wholesale adoption.
  D. translate — translations merge over the aligned window BY _msgId, so a
                 tail window smaller than the local array cannot misalign
                 (an index-based merge would write msg2's 译文 onto msg1).
  E. no-change — identical tail → returns false, rev still advances, and
                 saveConversations is NOT called (no repaint storm).
  F. legacy    — a non-windowed (full) response keeps the legacy Case-1
                 wholesale adoption byte-for-byte (regression pin).

NEUTER: force the anchor-miss branch to pretend-found on a COPY → (C) fails
(no escalation refetch), everything else stays green.

Drives the REAL shipped cross_tab_sync.js + conv_reducers.js + conv_window.js
under bare node. Skips cleanly when node isn't installed.
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

// ── Load-time shims cross_tab_sync.js touches (mirrors the notify-push
//    harness surface). ──
global._syncChannel = null;
global.TAB_ID = 'tab-test';
global.debugLog = () => {};
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.activeConvId = 'some-other-conv';   // skip the DOM repaint branch
global.addEventListener = () => {};
global.document = { visibilityState: 'visible', addEventListener: () => {} };
let _timers = [];
global.setTimeout = (fn, ms) => { _timers.push(fn); return _timers.length; };
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};
global.ConvCache = { remove: () => {}, put: () => {}, get: async () => null };
global.loadConversation = () => {};
global.newChat = () => {};
global.renderConversationList = () => {};
global.ConvView = { replaceAll: () => {} };
global._applySettingsToConv = () => {};
global._restoreConvToolState = () => {};
global._reconnectServerTaskIfIdle = () => false;
global.updateSendButton = () => {};
global.loadConversationsFromServer = async () => {};
global.pushIsConnected = () => true;
global.pushSubscribe = () => {};
global.TOFU_CONV_WINDOW = 60;

// ── Response queue + call recorder for Api.conversations.get. ──
let _getQueue = [];
let _getCalls = [];
let _saveCalls = [];
global.saveConversations = (id) => { _saveCalls.push(id); };
global.Api = { conversations: { get: async (id, opts) => {
  _getCalls.push({ id, opts });
  return _getQueue.shift() || null;
} } };

// REAL modules in bundle order: reducers (merge helpers) → conv_window
// (convWindowParam / recordWindowState) → cross_tab_sync (the code under test).
eval(fs.readFileSync(process.argv[2], 'utf8'));   // core/conv_reducers.js
eval(fs.readFileSync(process.argv[3], 'utf8'));   // conv_window.js
eval(fs.readFileSync(process.argv[4], 'utf8'));   // core/cross_tab_sync.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function reset() {
  _getQueue = []; _getCalls = []; _saveCalls = [];
  global.conversations = [];
}
const M = (id, role, content, extra) => Object.assign(
  { _msgId: id, role, content: content || '' }, extra || {});

(async () => {
  // ── A: windowed growth → APPEND, identities preserved, envelope counts ──
  reset();
  const a1 = M('a', 'user', 'q1');
  const b1 = M('b', 'assistant', 'answer-1');
  const convA = { id: 'convA', title: 'A', messages: [a1, b1], _serverRev: 10 };
  global.conversations = [convA];
  _getQueue = [{
    windowed: true, totalCount: 3, firstLoadedSeq: 1, lastLoadedSeq: 3,
    hasMore: false, rev: 11, title: 'A', updatedAt: 999,
    messages: [M('a', 'user', 'q1'), M('b', 'assistant', 'answer-1'),
               M('c', 'assistant', 'answer-2')],
  }];
  let r = await _verifyActiveConvFromServer('convA');
  check('A_returns_true', r === true);
  check('A_appended', convA.messages.length === 3 && convA.messages[2]._msgId === 'c');
  check('A_identity_preserved', convA.messages[0] === a1 && convA.messages[1] === b1);
  check('A_envelope_counts', convA._serverMsgCount === 3 && convA._serverRev === 11);
  check('A_persisted', _saveCalls.includes('convA'));
  check('A_window_param_sent', _getCalls.length === 1
        && _getCalls[0].opts && _getCalls[0].opts.query
        && _getCalls[0].opts.query.window === '60');

  // ── B: windowed in-place growth on the anchor pair (Case-2 semantics) ──
  reset();
  const b2 = M('b', 'assistant', 'short');
  const convB = { id: 'convB', title: 'B', messages: [M('a', 'user', 'q'), b2], _serverRev: 5 };
  global.conversations = [convB];
  _getQueue = [{
    windowed: true, totalCount: 2, firstLoadedSeq: 1, lastLoadedSeq: 2,
    hasMore: false, rev: 6, updatedAt: 900,
    messages: [M('a', 'user', 'q'), M('b', 'assistant', 'short — now much longer')],
  }];
  r = await _verifyActiveConvFromServer('convB');
  check('B_tail_growth_adopted', r === true && b2.content === 'short — now much longer');

  // ── C: anchor missing → exactly ONE escalation to a full read ──
  reset();
  const convC = { id: 'convC', title: 'C', messages: [M('zz', 'assistant', 'local')], _serverRev: 3 };
  global.conversations = [convC];
  const fullC = [M('zz', 'assistant', 'local'), M('n1', 'assistant', 'new')];
  _getQueue = [
    { windowed: true, totalCount: 30, firstLoadedSeq: 25, lastLoadedSeq: 30,
      hasMore: true, rev: 4, updatedAt: 800,
      messages: [M('x25', 'assistant', 's25'), M('x26', 'assistant', 's26')] },
    { messages: fullC, rev: 4, title: 'C', updatedAt: 800 },   // full (window:'0') response
  ];
  r = await _verifyActiveConvFromServer('convC');
  check('C_escalated_once', _getCalls.length === 2
        && _getCalls[1].opts && _getCalls[1].opts.query
        && _getCalls[1].opts.query.window === '0');
  check('C_legacy_adopt', r === true && convC.messages === fullC);

  // ── D: translations merge BY _msgId — no index misalignment ──
  reset();
  const d1 = M('m1', 'assistant', 'first answer');
  const d2 = M('m2', 'assistant', 'second answer');
  const convD = { id: 'convD', title: 'D', messages: [d1, d2], _serverRev: 7 };
  global.conversations = [convD];
  _getQueue = [{
    // Tail window of ONE — m1 is NOT in the window. An index-aligned merge
    // would compare m1 (local[0]) against the m2 server copy and, had the
    // contents matched, stamp m2's 译文 onto m1.
    windowed: true, totalCount: 2, firstLoadedSeq: 2, lastLoadedSeq: 2,
    hasMore: true, rev: 8, updatedAt: 700,
    messages: [M('m2', 'assistant', 'second answer', { translatedContent: '第二个回答' })],
  }];
  r = await _verifyActiveConvFromServer('convD');
  check('D_translation_landed', r === true && d2.translatedContent === '第二个回答');
  check('D_no_misalignment', d1.translatedContent === undefined);

  // ── E: no change → false, rev advances, no persist ──
  reset();
  const e1 = M('b', 'assistant', 'same');
  const convE = { id: 'convE', title: 'E', messages: [M('a', 'user', 'q'), e1], _serverRev: 20 };
  global.conversations = [convE];
  _getQueue = [{
    windowed: true, totalCount: 2, firstLoadedSeq: 1, lastLoadedSeq: 2,
    hasMore: false, rev: 21, updatedAt: 600,
    messages: [M('a', 'user', 'q'), M('b', 'assistant', 'same')],
  }];
  r = await _verifyActiveConvFromServer('convE');
  check('E_returns_false', r === false);
  check('E_rev_advanced', convE._serverRev === 21);
  check('E_no_persist', _saveCalls.length === 0);

  // ── F: full (non-windowed) response → legacy Case-1 wholesale adopt ──
  reset();
  const convF = { id: 'convF', title: 'F', messages: [M('a', 'user', 'q')], _serverRev: 1 };
  global.conversations = [convF];
  const fullF = [M('a', 'user', 'q'), M('b', 'assistant', 'full')];
  _getQueue = [{ messages: fullF, rev: 2, title: 'F', updatedAt: 500 }];
  r = await _verifyActiveConvFromServer('convF');
  check('F_legacy_wholesale', r === true && convF.messages === fullF);

  console.log(out.join('\n'));
  process.exit(0);
})().catch((e) => { console.log('HARNESS-ERROR ' + (e && e.stack || e)); process.exit(1); });
"""


def _run_harness(*js_paths: str) -> subprocess.CompletedProcess:
    harness = os.path.join(HERE, '_conv_verify_windowed_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        return subprocess.run(
            ['node', harness, *js_paths],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        try:
            os.remove(harness)
        except OSError:
            pass


def _sources(sync_src_path: str):
    return (
        os.path.join(JS_DIR, 'core', 'conv_reducers.js'),
        os.path.join(JS_DIR, 'conv_window.js'),
        sync_src_path,
    )


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_windowed_verify_adoption():
    proc = _run_harness(*_sources(os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    assert 'HARNESS-ERROR' not in output, output
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'windowed verify failures:\n' + output
    assert output.count('PASS') >= 15, f'expected >=15 PASS lines, got:\n{output}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_windowed_verify_escalate_neuter(tmp_path):
    """NEUTER: replace the anchor-miss 'escalate' with pretend-found on a COPY
    → (C) fails (no escalation refetch; the wrong anchor pair is adopted),
    every other check stays green. Proves (C) discriminates the escalate.
    Shipped file left byte-identical."""
    sync_js = os.path.join(JS_DIR, 'core', 'cross_tab_sync.js')
    with open(sync_js, encoding='utf-8') as f:
        src = f.read()

    needle = "  if (localMsgs.length > 0 && anchorIdx < 0) {\n    return 'escalate';\n  }"
    assert needle in src, 'escalate fragment drifted — update the neuter target'
    neutered = src.replace(
        needle,
        "  if (localMsgs.length > 0 && anchorIdx < 0) {\n"
        "    anchorIdx = serverMsgs.length - 1;   /* neutered: pretend-found */\n  }",
        1,
    )
    copy = tmp_path / 'cross_tab_sync_no_escalate.js'
    copy.write_text(neutered, encoding='utf-8')

    proc = _run_harness(*_sources(str(copy)))
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert 'FAIL C_escalated_once' in fails, (
        'NEUTER did not bite the escalate check:\n' + output)
    assert 'FAIL C_legacy_adopt' in fails, (
        'NEUTER should also break the legacy-adopt half of (C):\n' + output)

    with open(sync_js, encoding='utf-8') as f:
        assert f.read() == src, 'harness mutated the shipped cross_tab_sync.js'

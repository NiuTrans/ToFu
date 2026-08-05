"""jsdom regression: the queued user bubble is a FIRST-CLASS timeline row —
it must survive the whole queue lifecycle without a manual refresh.

Root fix (epic pt_cfdfd30c8699407b, 2026-08-05). Before this, the send
pipeline's queued branch SPLICED the optimistic user bubble out of
conv.messages ("queue bar only"), so the user's own message vanished the
moment the server answered {queued:true} and only reappeared when the
dispatched turn was re-fetched — the reported "user bubble suddenly
disappears, comes back after refresh once the agent finished".

The lifecycle this suite pins:

  queue   — the bubble STAYS, tagged `_pendingQueued` (+ session `_queueId`);
  sync    — `_lightMessageForSync` keeps `_pendingQueued` (the display marker
            the backend's own mirror row carries) but strips `_queueId`
            (session bookkeeping, never on the wire);
  replace — wholesale-replace lanes re-append a still-queued local row the
            server body legitimately lacks (`_withPendingQueuedTail`);
  dispatch— the server's reconciled row drops the marker; the local bubble
            flips back to normal via `_reconcileQueuedMarkers`, no reload.

HARNESS: drives the REAL shipped conv family under bare node
(tests/_conv_bundle_sources.py::conv_family_sources), plus a Phase-2
loadConversationMessages pass for the rescue and cache-fresh sweep halves.

NEUTER: strip `_reconcileQueuedMarkers` + `_withPendingQueuedTail` to
no-ops → the preservation check (A) and the marker-flip check (G) both red.
"""

from __future__ import annotations

import os
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_DIR = os.path.join(ROOT, 'static', 'js')

sys.path.insert(0, HERE)
from _jsdom import run_harness, frontend_module_guard  # noqa: E402
from _conv_bundle_sources import conv_family_sources  # noqa: E402

frontend_module_guard(need_jsdom=False)

_HARNESS = r"""
const fs = require('fs');
global.window = global;

global.activeConvId = 'c1';
global.activeStreams = new Map();
global._editingMsgIdx = null;
global.debugLog = () => {};
global.config = {};
global.renderConversationList = () => {};
global.renderChat = () => {};
global.showStreamingUIForConv = () => {};
global._restoreConvToolState = () => {};
global.Icon = () => '';
global.AbortSignal = { timeout: () => undefined };
global.apiUrl = (p) => p;
let CACHED = null;
global.ConvCache = {
  isAvailable: () => true,
  get: async () => CACHED,
  getMeta: async () => null,
  getAllMeta: async () => [],
  put: async () => {},
  remove: async () => {},
};
global.getActiveConv = () => conversations.find((c) => c.id === activeConvId) || null;
global._convSorter = (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0);
global.ConvView = { replaceAll: () => {}, applyMessage: () => true };
global.saveConversations = () => {};
global.document = { getElementById: () => null, querySelector: () => null, querySelectorAll: () => [] };

const M0 = { role: 'user', content: 'first question', _msgId: 'm0', timestamp: 1000 };
const M1 = { role: 'assistant', content: 'first answer', _msgId: 'm1', timestamp: 2000, finishReason: 'stop', _taskId: 'tOLD' };
let SERVER = { messages: [M0, M1], rev: 9, updatedAt: 3000 };
global.Api = {
  conversations: {
    getResponse: async () => ({
      ok: true, status: 200,
      headers: { get: () => null },
      json: async () => SERVER,
    }),
    get: async () => SERVER,
    put: async () => ({ ok: true, json: async () => ({ ok: true }) }),
  },
};

global.conversations = [];
const _files = [process.argv[2], ...process.argv.slice(4)];  // argv[3] is ROOT
for (const f of _files) eval(fs.readFileSync(f, 'utf8'));
global.conversations = conversations;

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

function seed() {
  conversations.length = 0;
  conversations.push({
    id: 'c1', title: 'c1', messages: [M0, M1],
    _serverMsgCount: 2, _needsLoad: true,
    createdAt: 1000, updatedAt: 2000, activeTaskId: null,
  });
}

(async () => {
  const need = ['_withPendingQueuedTail', '_reconcileQueuedMarkers',
                '_mergeQueuedMarkerOff', '_lightMessageForSync',
                'loadConversationMessages'];
  const missing = need.filter((n) => typeof global[n] !== 'function'
                                && typeof eval(n) !== 'function');
  check('fn_exposed', missing.length === 0);
  if (missing.length) { console.log('missing: ' + missing.join(',')); }

  /* ── A. wholesale-replace preserves a local-only queued row ── */
  {
    const local = [M0, M1,
      { role: 'user', content: 'queued q', _msgId: 'q1', timestamp: 3000,
        _pendingQueued: true, _queueId: 'qq1' }];
    const merged = _withPendingQueuedTail(local, [M0, M1]);
    check('A_replace_preserves_queued',
      merged.length === 3 && merged[2]._msgId === 'q1' && merged[2]._pendingQueued === true);
  }

  /* ── B. a server-carried twin (same _msgId) is NOT duplicated ── */
  {
    const serverTwin = { role: 'user', content: 'queued q', _msgId: 'q1',
                         timestamp: 3000, _pendingQueued: true };
    const local = [M0, M1,
      { role: 'user', content: 'queued q', _msgId: 'q1', timestamp: 3000,
        _pendingQueued: true, _queueId: 'qq1' }];
    const merged = _withPendingQueuedTail(local, [M0, M1, serverTwin]);
    check('B_server_twin_not_duplicated',
      merged.length === 3 && merged[2] === serverTwin);
  }

  /* ── C. dispatch flips the marker off (server twin lost it) ── */
  {
    const lm = { role: 'user', content: 'queued q', _msgId: 'q1',
                 timestamp: 3000, _pendingQueued: true, _queueId: 'qq1' };
    const sm = { role: 'user', content: 'queued q', _msgId: 'q1', timestamp: 3000 };
    const flips = _reconcileQueuedMarkers([M0, M1, sm], [M0, M1, lm]);
    check('C_dispatch_clears_marker',
      flips === 1 && lm._pendingQueued === undefined && lm._queueId === undefined);
  }

  /* ── D. still queued server-side → marker kept ── */
  {
    const lm = { role: 'user', content: 'q', _msgId: 'q1',
                 _pendingQueued: true, _queueId: 'qq1' };
    const sm = { role: 'user', content: 'q', _msgId: 'q1', _pendingQueued: true };
    const flips = _reconcileQueuedMarkers([sm], [lm]);
    check('D_still_queued_keeps_marker', flips === 0 && lm._pendingQueued === true);
  }

  /* ── E. the PUT wire keeps the display marker, drops session bookkeeping ── */
  {
    const wire = _lightMessageForSync({
      role: 'user', content: 'queued q', _msgId: 'q1', timestamp: 3000,
      _pendingQueued: true, _queueId: 'qq1' });
    check('E_wire_keeps_marker_strips_queueId',
      wire._pendingQueued === true && wire._queueId === undefined);
  }

  /* ── F. Phase-2 server-shorter fetch: the queued tail is rescued ── */
  {
    seed();
    CACHED = null;                       // cache miss → cacheHit=false
    SERVER = { messages: [M0, M1], rev: 9, updatedAt: 3000 };
    const conv = conversations[0];
    conv.messages.push({ role: 'user', content: 'queued q', _msgId: 'q1',
                         timestamp: 3000, _pendingQueued: true, _queueId: 'qq1' });
    await loadConversationMessages('c1');
    const tail = conv.messages[conv.messages.length - 1];
    check('F_phase2_rescue_keeps_queued',
      conv.messages.length === 3 && tail._msgId === 'q1' && tail._pendingQueued === true);
  }

  /* ── G. Phase-2 cache-fresh sweep: dispatched turn loses the badge ── */
  {
    seed();
    const qLocal = { role: 'user', content: 'queued q', _msgId: 'q1',
                     timestamp: 3000, _pendingQueued: true, _queueId: 'qq1' };
    CACHED = { messages: [M0, M1, qLocal], title: 'c1', updatedAt: 5000 };
    const qServer = { role: 'user', content: 'queued q', _msgId: 'q1', timestamp: 3000 };
    SERVER = { messages: [M0, M1, qServer], rev: 10, updatedAt: 5000 };
    await loadConversationMessages('c1');
    const conv = conversations[0];
    const row = conv.messages.find((m) => m._msgId === 'q1');
    check('G_cache_fresh_marker_flips',
      !!row && row._pendingQueued === undefined && row._queueId === undefined);
  }

  console.log(out.join('\n'));
  console.log('__JSDOM_RESULT__ ' + JSON.stringify({
    pass: out.filter(l => l.startsWith('PASS')).length,
    fail: out.filter(l => l.startsWith('FAIL')).length,
  }));
  process.exit(0);
})();
"""


def _sources(*, neuter=None):
    override = None
    if neuter:
        target = os.path.join(JS_DIR, 'core', 'conv_reducers.js')
        src = open(target, encoding='utf-8').read()
        if neuter == 'queued_reducers':
            n1 = ("function _reconcileQueuedMarkers(serverMsgs, localMsgs) {\n"
                  "  if (!Array.isArray(serverMsgs) || !Array.isArray(localMsgs)) return 0;")
            assert src.count(n1) == 1, 'marker-sweep reducer drifted — update the neuter target'
            src = src.replace(
                n1,
                "function _reconcileQueuedMarkers(serverMsgs, localMsgs) {\n"
                "  return 0;  // NEUTERED\n"
                "  if (!Array.isArray(serverMsgs) || !Array.isArray(localMsgs)) return 0;",
                1)
            n2 = ("function _withPendingQueuedTail(localMsgs, serverMsgs) {\n"
                  "  const out = Array.isArray(serverMsgs) ? serverMsgs : [];")
            assert src.count(n2) == 1, 'preserve-tail reducer drifted — update the neuter target'
            src = src.replace(
                n2,
                "function _withPendingQueuedTail(localMsgs, serverMsgs) {\n"
                "  return Array.isArray(serverMsgs) ? serverMsgs : [];  // NEUTERED\n"
                "  const out = Array.isArray(serverMsgs) ? serverMsgs : [];",
                1)
        else:  # pragma: no cover
            raise ValueError(neuter)
        copy = os.path.join(HERE, '_conv_reducers_neutered.js')
        with open(copy, 'w') as f:
            f.write(src)
        override = {'core/conv_reducers.js': copy}
    return conv_family_sources(override=override)


def test_queued_bubble_full_lifecycle():
    srcs = _sources()
    run_harness(
        target_js=srcs[0],
        extra_targets=srcs[1:],
        body_js=_HARNESS,
        expect_pass=8,
        label='queued-bubble-lifecycle',
    )


def test_NC_queued_reducers_are_load_bearing():
    """NEUTER both queued-bubble reducers: preservation (A) and the
    cache-fresh marker flip (G) must both FAIL — proving the suite's checks
    flow through the real reducers, not some incidental state."""
    import subprocess
    import tempfile
    srcs = _sources(neuter='queued_reducers')
    with tempfile.NamedTemporaryFile('w', suffix='.js', dir=HERE, delete=False) as fh:
        hp = fh.name
        fh.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', hp, *srcs], capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': os.path.join(HERE, '_jsdom_harness.js')})
    finally:
        os.remove(hp)
        _neu = os.path.join(HERE, '_conv_reducers_neutered.js')
        if os.path.exists(_neu):
            os.remove(_neu)
    out = proc.stdout
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{out}'
    assert 'FAIL A_replace_preserves_queued' in out, (
        'NEUTER did not bite: preservation passed without _withPendingQueuedTail.\n' + out)
    assert 'FAIL G_cache_fresh_marker_flips' in out, (
        'NEUTER did not bite: marker flip passed without _reconcileQueuedMarkers.\n' + out)


# ── Source-level wiring pins (the lanes that must call the reducers) ──

def _src(rel):
    with open(os.path.join(JS_DIR, rel), encoding='utf-8') as f:
        return f.read()


def test_wiring_queued_branch_tags_instead_of_splicing():
    src = _src(os.path.join('main', 'main_send_pipeline.js'))
    start = src.index('if (result.queued) {')
    end = src.index('_refreshServerQueue(convId);', start)
    lane = src[start:end]
    assert 'userMsg._pendingQueued = true' in lane, (
        'the queued branch no longer tags the bubble — it must stay in the '
        'timeline as a _pendingQueued row, not be spliced out')
    assert 'splice(userMsgIdx' not in lane, (
        'REGRESSION: the queued branch splices the optimistic bubble out of '
        'conv.messages again — the vanishing-user-bubble bug is back')
    assert 'ConvView.apply' in lane, (
        'the queued branch must re-render the bubble in place so the badge shows')


def test_wiring_cancel_removes_the_timeline_row():
    src = _src(os.path.join('main', 'main_send_pipeline.js'))
    start = src.index('function removePendingQueueItem')
    end = src.index('function clearPendingQueue', start)
    lane = src[start:end]
    assert '_queueId === queueId' in lane and 'splice(_qi, 1)' in lane, (
        'cancel must remove the _pendingQueued timeline row too — otherwise a '
        'cancelled queued message strands as a greyed bubble forever')


def test_wiring_wholesale_lanes_preserve_queued_rows():
    conv = _src(os.path.join('core', 'conversations.js'))
    assert '_withPendingQueuedTail(conv.messages, serverMsgs)' in conv, (
        'the ghost-adopt wholesale replace must preserve still-queued rows')
    cts = _src(os.path.join('core', 'cross_tab_sync.js'))
    assert '_withPendingQueuedTail(localMsgs, serverMsgs)' in cts, (
        'the notify Case-1 adopt must preserve still-queued rows')
    push = _src('conv_sync_push.js')
    assert '_withPendingQueuedTail(conv.messages, serverMsgs)' in push, (
        'the history_rewrite adopt must preserve still-queued rows')


def test_wiring_refresh_restamps_queueid_by_timestamp():
    """After a reload the timeline row is the server mirror — it carries the
    same timestamp but no session `_queueId`. `_refreshServerQueue` must
    re-stamp it (queue row created_at == user msg timestamp), or the
    queue-bar cancel cannot find the local row to remove."""
    src = _src(os.path.join('main', 'main_send_pipeline.js'))
    start = src.index('async function _refreshServerQueue')
    lane = src[start:]
    assert '_qidByTs' in lane and 'm._queueId = _qidByTs.get(m.timestamp)' in lane, (
        'the _queueId re-stamp on refresh is gone — cancel-after-reload '
        'would leave the greyed bubble behind')


def test_wiring_convview_applymessage_exists():
    """The notify/verify lanes repaint via window.ConvView.applyMessage — a
    method that NEVER EXISTED until this epic (the typeof guard made every
    field-level repaint a silent no-op)."""
    src = _src('conv_view.js')
    assert 'applyMessage' in src, (
        'ConvView.applyMessage is gone — _streamActiveVerify field repaints '
        '(translations, terminal metadata, queued-marker flips) silently no-op')

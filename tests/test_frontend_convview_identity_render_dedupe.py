"""tests/test_frontend_convview_identity_render_dedupe.py — regression for the
RENDER-layer "one data entry, two identical bubbles" duplicate class.

WHY
---
Settled assistant bubbles are keyed ``id="msg-${idx}"`` (mutable ARRAY
POSITION); the live turn is the ``#streaming-msg`` singleton. On a reconnect,
``connectToTask`` used to evict the prior static bubble by INDEX
(``getElementById('msg-' + lastIdx)``). When the tail's index had DRIFTED (a
placeholder push / splice / lazy-window offset), that eviction MISSED the real
static node — a fresh ``#streaming-msg`` was inserted alongside it and later
finalized into a second ``msg-M`` node. Two identical DOM bubbles then rendered
for ONE ``conv.messages`` entry (self-healing on refresh, because the full
``innerHTML`` rebuild is a faithful projection — proving it is render-only).

THE FIX (identity-keyed projection — invariant: one ``_msgId`` ⇒ at most one
DOM node in ``#chatInner``):
  1. ``ConvView.startStreaming`` is the single seam for inserting the streaming
     bubble; before inserting it evicts ANY node carrying the same
     ``data-msg-id`` (``_evictByMsgId``), regardless of its ``msg-N`` index.
  2. ``ConvView.finalizeStreaming`` sweeps, after the outerHTML swap, every
     OTHER node sharing that ``data-msg-id`` — a belt so a stranded twin can
     never survive finalize.

This harness loads the REAL ``static/js/conv_view.js`` under jsdom, seeds a
static bubble at a DRIFTED index, drives startStreaming + finalizeStreaming,
and asserts exactly ONE node exists for the ``_msgId``. The NEUTER re-evals a
copy of the source with ``_evictByMsgId`` disabled and proves the duplicate
(two nodes) returns.
"""

from __future__ import annotations

import os

from tests._jsdom import run_harness, JS_DIR, ROOT

CONV_VIEW = os.path.join(JS_DIR, 'conv_view.js')


_BODY = r"""
const fs = require('fs');
const { setup } = require(process.env.JSDOM_HARNESS);

const CONV_VIEW = process.argv[2];   // argv[2] = target_js (conv_view.js)

// Realistic-enough renderers: BOTH stamp data-msg-id (the identity key) plus
// the settled node's msg-${idx} id / the live node's #streaming-msg id — the
// exact contract chat_render.js (id="msg-${idx}" + data-msg-id) and
// _streamingBubbleHTML (#streaming-msg + data-msg-id) emit in production.
function renderMessage(msg, idx) {
  const mid = msg && msg._msgId ? ` data-msg-id="${msg._msgId}"` : '';
  return `<div class="message" id="msg-${idx}"${mid}><div class="message-body">${(msg && msg.content) || ''}</div></div>`;
}
function _streamingBubbleHTML(role, status, time, msgId) {
  const mid = msgId ? ` data-msg-id="${msgId}"` : '';
  return `<div class="message" id="streaming-msg"${mid}><div class="message-body" id="streaming-body">${status || ''}</div></div>`;
}

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [CONV_VIEW],   // evals the REAL conv_view.js → window.ConvView (fixed)
  globals: {
    activeConvId: 'c1',
    conversations: [],
    renderMessage,
    _streamingBubbleHTML,
    _convRenderFingerprint: () => '',
    _lastRenderedFingerprint: '',
    _ensureMsgId: (m) => { if (m && !m._msgId) m._msgId = 'tmp_x'; return m; },
  },
});

const inner = () => document.getElementById('chatInner');
function countByMsgId(id) {
  return inner().querySelectorAll('[data-msg-id="' + id + '"]').length;
}
// Reset DOM + model to a fresh conv whose assistant turn (msgId 'm1') lives at
// array index 1, while a STALE static bubble for the SAME _msgId is stranded in
// the DOM at a DRIFTED index (msg-3). The index-based eviction would look for
// msg-1 and miss msg-3 entirely.
function seedDrift() {
  const assistant = { role: 'assistant', content: 'the one reply', _msgId: 'm1' };
  // conv_view.js's _findConv reads the BARE `conversations` global (resolved on
  // globalThis in the indirect-eval scope), not window.conversations — set both.
  const convs = [{ id: 'c1', messages: [
    { role: 'user', content: 'q', _msgId: 'u1' },
    assistant,
  ] }];
  globalThis.conversations = window.conversations = convs;
  // Static bubble stranded at msg-3 (drifted index) — NOT msg-1.
  inner().innerHTML = '<div class="message" id="msg-3" data-msg-id="m1"><div class="message-body">the one reply</div></div>';
  return assistant;
}

// ── Scenario 1: startStreaming evicts the drifted static twin at INSERT. ──
(function () {
  const assistant = seedDrift();
  check('s1_precondition_drifted_static_present',
    !!document.getElementById('msg-3') && countByMsgId('m1') === 1);
  window.ConvView.startStreaming('c1', { role: 'worker', status: 'Resuming…', msgId: 'm1' });
  // The drifted static bubble must be gone; the streaming bubble is the sole m1 node.
  check('s1_drifted_static_evicted', !document.getElementById('msg-3'));
  check('s1_streaming_bubble_present', !!document.getElementById('streaming-msg'));
  check('s1_single_node_for_msgid_after_insert', countByMsgId('m1') === 1);
})();

// ── Scenario 2: finalize sweep removes a twin the insert path DID NOT catch. ──
//   Simulate a stranded static twin coexisting with an already-live streaming
//   bubble (e.g. inserted by a path that bypassed startStreaming), then finalize.
(function () {
  const assistant = seedDrift();
  // Live streaming bubble for m1 ALONGSIDE the stranded static msg-3(m1).
  inner().insertAdjacentHTML('beforeend',
    '<div class="message" id="streaming-msg" data-msg-id="m1"><div class="message-body" id="streaming-body">…</div></div>');
  check('s2_precondition_two_nodes', countByMsgId('m1') === 2);
  window.ConvView.finalizeStreaming('c1', assistant);
  // After finalize: the streaming bubble became msg-1; the sweep removed the
  // stranded msg-3 twin → exactly one node, and it is the finalized static one.
  check('s2_finalized_static_present', !!document.getElementById('msg-1'));
  check('s2_stale_twin_swept', !document.getElementById('msg-3'));
  check('s2_single_node_for_msgid_after_finalize', countByMsgId('m1') === 1);
  check('s2_no_streaming_left', !document.getElementById('streaming-msg'));
})();

// ── Scenario 3 (FIXED end-to-end): drift → startStreaming → finalize = 1 node. ──
(function () {
  const assistant = seedDrift();
  window.ConvView.startStreaming('c1', { role: 'worker', status: 'Resuming…', msgId: 'm1' });
  window.ConvView.finalizeStreaming('c1', assistant);
  check('s3_fixed_e2e_single_node', countByMsgId('m1') === 1);
})();

// ── NEUTER: re-eval conv_view.js with _evictByMsgId disabled → duplicate returns. ──
(function () {
  let src = fs.readFileSync(CONV_VIEW, 'utf8');
  const before = src;
  // Force the identity-eviction primitive to a no-op (unreachable tail is legal JS).
  src = src.replace(
    'if (!inner || !msgId) return 0;\n    var sel;',
    'if (!inner || !msgId) return 0;\n    return 0; /* NEUTER */\n    var sel;'
  );
  if (src === before) {
    check('neuter_patch_applied', false);   // anchor drifted — fail loudly
    report();
    return;
  }
  (0, eval)(src);   // overwrite window.ConvView with the neutered closure
  const assistant = seedDrift();
  window.ConvView.startStreaming('c1', { role: 'worker', status: 'Resuming…', msgId: 'm1' });
  window.ConvView.finalizeStreaming('c1', assistant);
  // With eviction+sweep disabled, the drifted static twin survives alongside
  // the finalized node → the render duplicate is back.
  check('neuter_patch_applied', true);
  check('neuter_duplicate_returns_two_nodes', countByMsgId('m1') === 2);
})();

report();
"""


def test_convview_enforces_one_node_per_msgid():
    output = run_harness(
        target_js=CONV_VIEW,
        body_js=_BODY,
        min_pass=11,
        label='convview-identity-dedupe',
    )
    # Fixed-path invariants:
    assert 'PASS s1_drifted_static_evicted' in output, output
    assert 'PASS s1_single_node_for_msgid_after_insert' in output, output
    assert 'PASS s2_stale_twin_swept' in output, output
    assert 'PASS s2_single_node_for_msgid_after_finalize' in output, output
    assert 'PASS s3_fixed_e2e_single_node' in output, output
    # NEUTER proves the eviction/sweep is load-bearing (duplicate returns):
    assert 'PASS neuter_patch_applied' in output, output
    assert 'PASS neuter_duplicate_returns_two_nodes' in output, output


def test_source_carries_identity_keyed_render_seam():
    """The shipped source must actually contain the identity-keyed seam, so
    this regression rots with the code — not just with the harness copy."""
    with open(CONV_VIEW, encoding='utf-8') as f:
        cv = f.read()
    assert 'function _evictByMsgId(' in cv, \
        '_evictByMsgId identity-eviction primitive missing from conv_view.js'
    assert 'startStreaming: function' in cv, \
        'ConvView.startStreaming insert seam missing from conv_view.js'
    # finalizeStreaming must sweep by identity after the swap.
    #
    # Anchored on the BEHAVIOUR, not on a local variable's spelling. The old
    # form pinned the literal `_evictByMsgId(_inner, msg._msgId, _keep)`, which
    # went red purely because the keep-node local was renamed when the swap
    # moved from `outerHTML` to `replaceWith` — the sweep itself never left.
    # Worse, that spelling ENCODED the very bug the rename fixed: `_keep` was
    # `getElementById('msg-' + idx)`, which right after the swap can match TWO
    # nodes (the fresh one and a stale bubble at the same slot after an index
    # shift) and returns the FIRST — so the sweep evicted the node it had just
    # restored. Pin what must stay true instead: the sweep runs on this msgId,
    # and its keep-node is NOT re-found by positional id.
    _fin = cv[cv.index('finalizeStreaming: function'):]
    assert '_evictByMsgId(_inner, msg._msgId,' in _fin, \
        'finalizeStreaming no longer sweeps twins by data-msg-id after the swap'
    _i_sweep = _fin.index('_evictByMsgId(_inner, msg._msgId,')
    assert "getElementById('msg-' + idx)" not in _fin[max(0, _i_sweep - 400):_i_sweep + 120], \
        ("finalizeStreaming's sweep resolves its keep-node by POSITIONAL id "
         "again — after the swap that id can match two nodes and getElementById "
         "returns the first, so the sweep deletes the bubble it just restored "
         "(observed: a failed Continue made the interrupted turn vanish). Hold "
         "the new node by reference (replaceWith) instead.")

    sse = os.path.join(ROOT, 'static', 'js', 'ui', 'sse_pipeline.js')
    with open(sse, encoding='utf-8') as f:
        sse_src = f.read()
    assert 'window.ConvView.startStreaming(convId' in sse_src, \
        'connectToTask reconnect no longer routes the streaming insert through ConvView.startStreaming'
    # The old index-based eviction must be gone (it was the drift-miss vector).
    assert 'const existing = document.getElementById(`msg-${lastIdx}`);' not in sse_src, \
        'the index-based `msg-${lastIdx}` eviction is back — reintroduces the drift-miss render duplicate'

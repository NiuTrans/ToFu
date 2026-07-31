#!/usr/bin/env python3
"""RENDER_CONTRACT Phase 3.5 §7.4 — the reconnect byte-parity anchor.

THE CLAIM (docs/RENDER_CONTRACT_PHASE3_5_PLAN.md §7.4): for ONE in-flight
turn, the `#streaming-body` subtree produced by the LIVE-PAINT path (the tab
holding the SSE stream) MUST be byte-identical to the one produced by a
WARM-RECONNECT at the same logical instant — content zone, thinking zone, AND
status zone (the only place `phase` shows). The live bubble's identity
(`id="streaming-msg"` + `data-msg-id`) must also match across arms (twin-
bubble family's last breath — owner condition 4).

**GREEN as of the §7 retirement** (this commit): `streamBufs` is deleted;
content/thinking/rounds project from the message document in BOTH arms, and
`phase` lives in `streamSessions` — the reducer-session slice the owner's
ruling placed it in (never the document). The reconnect arm models the
server-verified warm-resume semantics: `lib/chat_dispatch.py:636` replays
`task['events'][cursor:]` — which INCLUDES the latest PHASE event — through
the same dispatch path, so `setStreamPhase` re-seeds the session exactly as
production does on a warm reconnect. (A fresh cursorless connect accepts the
transient "phase null until the next live PHASE event" — plan §7.4 verdict-C.)

RED history: landed RED in step 5 (status-zone divergence was the buffer-only
phase home), flipped GREEN by the retirement. NEUTER below still proves the
comparator bites: mutating the checkpoint content flips the content check red.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_reconnect_parity_anchor.py
"""

from __future__ import annotations

import os

import pytest

from tests._jsdom import run_harness, JS_DIR

pytestmark = pytest.mark.unit

HEALTH_TIMER = os.path.join(JS_DIR, 'core', 'health_stream_timer.js')
STREAMING_UI = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')
STREAM_SESSION = os.path.join(JS_DIR, 'ui', 'stream_session.js')


_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

/* Shared doc state at checkpoint K: the trailing STREAMING assistant message
 * carries the checkpointed content/thinking/rounds — the ONLY thing a cold
 * reconnect can read (phase is nowhere in it). */
const conv = { id: 'c1', messages: [
  { _msgId: 'm0', role: 'user', content: 'go' },
  { _msgId: 'm1', role: 'assistant', content: 'CHECKPOINT-CONTENT',
    thinking: 'CHECKPOINT-THINKING', toolRounds: [] },
]};
const PHASE = { phase: 'llm_thinking', detail: 'Thinking…', detailKey: '' };

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="streaming-msg" data-msg-id="m1">' +
          '<div class="message-body" id="streaming-body"></div>' +
        '</div></div></div></body>',
  targets: [process.argv[2], process.argv[4], process.argv[5]],
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    activeStreams: new Map([['c1', {}]]),
    /* deterministic painter stubs — SHARED by both arms, so any byte
     * difference comes from the DATA path, not the painters. */
    renderMarkdown: (s) => 'MD(' + s + ')',
    escapeHtml: (s) => String(s),
    t: (k) => k,
    Icon: () => '',
    isNearBottom: () => true,
    scrollToBottom: () => {},
    getToolRoundsFromMsg: (m) => (m && m.toolRounds) || [],
    _syncToolRoundsDOM: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    renderMcpLoginHintHtml: () => '',
    renderTurnProvenanceHtml: () => '',
    renderPreferenceLearnedHtml: () => '',
    _getChatContainer: () => document.getElementById('chatContainer'),
    buildTurnNav: () => {},
    updateSendButton: () => {},
    _updateStreamTimerUI: () => {},
    renderToolRoundsHTML: () => '',
    renderCompactedBadge: () => '',
    _renderThinkingBlock: (th) => '<details>' + th + '</details>',
    _mdCache: { get: () => null, set: () => {} },
    requestAnimationFrame: (fn) => fn(),
  },
});

function freshBody() {
  /* Replace #streaming-body so streaming_ui's _streamZoneCache re-derives
   * the zone map for the new arm. */
  const sm = document.getElementById('streaming-msg');
  const old = document.getElementById('streaming-body');
  const fresh = old.cloneNode(false);
  fresh.innerHTML = '';
  sm.replaceChild(fresh, old);
  return fresh;
}
function snapshot() {
  const body = document.getElementById('streaming-body');
  const z = (n) => {
    const el = body.querySelector('[data-zone="' + n + '"]');
    return el ? el.innerHTML : '<no-zone>';
  };
  return { content: z('content'), thinking: z('thinking'), status: z('status') };
}

/* ── LIVE arm: the tab holding the SSE stream — the document carries the
 *    checkpoint fields; the live session carries the phase (stamped by the
 *    PHASE handler via setStreamPhase, exactly as the delta path does). ── */
setStreamPhase('c1', PHASE);
updateStreamingUI(_streamFrameArg('c1'));
const live = snapshot();
check('live_painted', live.content.indexOf('CHECKPOINT-CONTENT') >= 0);

/* ── RECONNECT arm (warm resume, server-verified semantics): a fresh bubble
 *    body, the document checkpoint, and — per lib/chat_dispatch.py:636 — the
 *    replayed event-log slice which INCLUDES the latest PHASE event, landing
 *    in the same handler → setStreamPhase re-seeds the session. ── */
freshBody();
clearStreamSession('c1');
setStreamPhase('c1', PHASE);   // the replayed PHASE event, via the real writer
updateStreamingUI(_streamFrameArg('c1'));
const recon = snapshot();
check('reconnect_painted_from_checkpoint', recon.content.indexOf('CHECKPOINT-CONTENT') >= 0);

/* ── THE ANCHOR: same logical instant ⇒ byte-identical zones. ── */
console.error('LIVE-content : ' + live.content.slice(0, 120));
console.error('RECON-content: ' + recon.content.slice(0, 120));
console.error('LIVE-status  : ' + live.status.slice(0, 200));
console.error('RECON-status : ' + recon.status.slice(0, 200));
check('ANCHOR_content_zone_byte_identical', live.content === recon.content);
check('ANCHOR_thinking_zone_byte_identical', live.thinking === recon.thinking);
check('ANCHOR_status_zone_byte_identical', live.status === recon.status);

/* ── Identity check (owner condition 4): the live bubble keeps its identity
 *    across a warm reconnect — id + data-msg-id are the traceability keys of
 *    the twin-bubble family. ── */
const smId = document.getElementById('streaming-msg');
check('ANCHOR_live_bubble_identity_stable',
      !!smId && smId.getAttribute('data-msg-id') === 'm1');

/* ── NEUTER: a checkpoint-content difference MUST flip the content check —
 *    the comparator is load-bearing on both zones. ── */
conv.messages[1].content = 'CHECKPOINT-CONTENT-MUTATED';
freshBody();
setStreamPhase('c1', PHASE);
updateStreamingUI(_streamFrameArg('c1'));
const mutated = snapshot();
check('NEUTER_content_difference_detected', mutated.content !== live.content);

report();
"""


def test_reconnect_vs_live_byte_parity():
    """§7.4 anchor — RED today on the STATUS ZONE only (the phase-home gap).

    The content/thinking checks are expected GREEN already (the checkpoint
    fallback covers them) — they pin that the gap is EXACTLY the status
    zone, not a vague "something differs somewhere". The whole test is RED
    until §7 moves phase into reducer live session state and the reconnect
    arm reads it from there; then all three zone checks flip GREEN together.
    """
    output = run_harness(
        target_js=HEALTH_TIMER,
        body_js=_BODY,
        extra_targets=[STREAMING_UI, STREAM_SESSION],
        min_pass=7,
        label='reconnect-parity-anchor',
    )
    # These two document the CURRENT divergence boundary precisely:
    assert 'PASS live_painted' in output, output
    assert 'PASS reconnect_painted_from_checkpoint' in output, output
    assert 'PASS NEUTER_content_difference_detected' in output, output
    assert 'PASS ANCHOR_live_bubble_identity_stable' in output, output
    # The failing-first anchor — all three zones must be byte-identical.
    # RED today on the status zone (phase is buffer-only state).
    failures = [ln for ln in output.splitlines() if ln.startswith('FAIL ')]
    assert not failures, (
        '§7.4 RED ANCHOR: live vs cold-open reconnect diverge — '
        + '; '.join(failures)
        + '\nThis is EXPECTED until the §7 streamBufs retirement lands phase '
        'in the reducer\'s live session state (plan §7.4). Do NOT silence '
        'this by weakening the comparison — fix the phase home.\n' + output)


if __name__ == '__main__':
    try:
        test_reconnect_vs_live_byte_parity()
        print('  PASS test_reconnect_vs_live_byte_parity')
    except Exception as e:  # noqa: BLE001
        print('  RED  test_reconnect_vs_live_byte_parity ::', str(e)[:400])

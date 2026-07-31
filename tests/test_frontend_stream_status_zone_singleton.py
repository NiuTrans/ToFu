#!/usr/bin/env python3
"""Guard: the streaming bubble's status zone is a SINGLETON across frames.

THE BUG (2026-07-25, owner screenshot — "chatinner 里 等待中… ×4 + 正在生成回复…
+ 已发送给 kimi-k3… + 推理中 3/100/142/…/387 字符 楼梯状堆叠"):

The default-shape streaming bubble (`_streamingBubbleHTML` at HEAD, role with
default status + no detail) seeds zones content/thinking/tool/fc/swarmInbox
but NO `[data-zone="status"]`. Because `[data-zone="tool"]` IS present,
`_ensureStreamZones` early-returns and the status zone is left to the
lazy-create branch in `updateStreamingUI` (streaming_ui.js). That branch
appended a fresh `<div data-zone="status">` on EVERY rAF frame because it
never wrote the created element back into `_streamZoneCache` — the cache kept
returning `status: null`, so the next frame appended ANOTHER one, painted
with that frame's phase. Result: one status box per frame, stacked (the
screenshot's staircase — each old box frozen at its own 推理中 N 字符).

THE FIX: `zones.status = statusZone;` after the append (zones IS
_streamZoneCache), so later frames reuse the single zone and the phase paint
updates it in place.

The harness builds the lazy-create PRECONDITION as a hardcoded fixture (a
bubble body with a tool zone but no status zone — the production default
shape; the fixture is deliberately NOT driven through `_streamingBubbleHTML`
because that template is a moving target under sibling churn — the pinned
contract is the BRANCH precondition, not the template) and drives the REAL
`updateStreamingUI` through three frames of one live turn: exactly ONE status
zone must exist, it must be the LIVE one (latest counter), and a fresh bubble
(cache re-derive) must stay singleton too. The NEUTER arm strips the
write-back line from a scratch copy and proves the singleton checks go RED —
the write-back is load-bearing, not decoration.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \\
       tests/test_frontend_stream_status_zone_singleton.py
"""

from __future__ import annotations

import os
import subprocess
import tempfile

import pytest

from tests._jsdom import (
    HERE,
    JS_DIR,
    ROOT,
    node_deps_available,
    run_harness,
)

pytestmark = pytest.mark.unit

STREAMING_UI = os.path.join(JS_DIR, 'ui', 'streaming_ui.js')
_SHARED_HARNESS_JS = os.path.join(HERE, '_jsdom_harness.js')

_FIX_LINE = '    zones.status = statusZone;'

_BODY = r"""
const { setup } = require(process.env.JSDOM_HARNESS);

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    isNearBottom: () => true,
    scrollToBottom: () => {},
    _syncToolRoundsDOM: () => {},
    _buildSwarmInboxChipsHTML: () => '',
  },
});

/* The lazy-create PRECONDITION, hardcoded: the production default-shape
 * streaming bubble (HEAD _streamingBubbleHTML, default status + no detail)
 * seeds these zones and NO [data-zone="status"]. The tool zone's presence is
 * what makes _ensureStreamZones early-return and leaves the status zone to
 * the lazy-create branch — pin BOTH halves of the precondition. */
const inner = document.getElementById('chatInner');
function seedBubble() {
  inner.innerHTML =
    '<div class="message streaming-message" id="streaming-msg">' +
      '<div class="message-body" id="streaming-body">' +
        '<div data-zone="content" class="stream-content"></div>' +
        '<div data-zone="thinking" class="stream-thinking" style="display:none"></div>' +
        '<div data-zone="tool" class="stream-tool"></div>' +
        '<div data-zone="fc" class="stream-fc"></div>' +
        '<div data-zone="swarmInbox" class="stream-swarm-inbox"></div>' +
      '</div>' +
    '</div>';
  return document.getElementById('streaming-body');
}
const body = seedBubble();
check('fixture_matches_lazy_create_precondition',
      !body.querySelector('[data-zone="status"]')
      && !!body.querySelector('[data-zone="tool"]'));

function frame(n) {
  updateStreamingUI({
    toolRounds: [], thinking: 'x'.repeat(n), content: '',
    phase: { phase: 'thinking_active', _thinkingLen: n },
  });
}

/* 1. THREE frames of ONE live turn (the screenshot's 推理中 3/100/142 字符). */
frame(3); frame(100); frame(142);
const zones = body.querySelectorAll('[data-zone="status"]');
check('single_status_zone_after_3_frames', zones.length === 1);
const ctr = body.querySelector('.stream-phase-counter');
check('sole_zone_is_the_live_one_counter_latest',
      !!ctr && ctr.textContent.indexOf(':142') >= 0);
check('sole_zone_painted_with_thinking_phase',
      zones.length === 1 && zones[0].innerHTML.indexOf('stream-phase') >= 0);

/* 2. Fresh bubble (cache re-derive on a new #streaming-body): two more frames
 *    must STILL yield exactly one status zone. */
const body2 = seedBubble();
frame(175); frame(239);
check('single_status_zone_fresh_bubble',
      body2.querySelectorAll('[data-zone="status"]').length === 1);

report();
"""


def _run_raw(streaming_ui_path: str) -> str:
    """Run the harness without asserting — used by the NEUTER arm to inspect
    FAIL lines on a deliberately-broken scratch copy."""
    if not node_deps_available():
        pytest.skip('node + jsdom dev-deps not installed (run `npm install`)')
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        harness_path = fh.name
        fh.write(_BODY)
    try:
        proc = subprocess.run(
            ['node', harness_path, streaming_ui_path, ROOT],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, 'JSDOM_HARNESS': _SHARED_HARNESS_JS},
        )
    finally:
        try:
            os.remove(harness_path)
        except OSError:
            pass
    assert proc.returncode == 0, f'node failed: {proc.stderr}'
    return (proc.stdout or '').strip()


def test_status_zone_is_singleton_across_frames():
    """Fixed code: exactly one status zone per bubble, updated in place."""
    output = run_harness(
        target_js=STREAMING_UI,
        body_js=_BODY,
        min_pass=5,
        label='status-zone-singleton',
    )
    for name in (
        'fixture_matches_lazy_create_precondition',
        'single_status_zone_after_3_frames',
        'sole_zone_is_the_live_one_counter_latest',
        'sole_zone_painted_with_thinking_phase',
        'single_status_zone_fresh_bubble',
    ):
        assert f'PASS {name}' in output, output


def test_NEUTER_writeback_is_load_bearing():
    """NEUTER: strip `zones.status = statusZone;` from a scratch copy — the
    singleton checks MUST go red (one box per frame returns), proving the
    write-back line is what stands between us and the staircase."""
    src = open(STREAMING_UI, encoding='utf-8').read()
    assert _FIX_LINE in src, (
        'fix line missing from streaming_ui.js — the guard no longer has '
        'anything to pin; re-check the fix.')
    neutered = src.replace(_FIX_LINE + '\n', '', 1)
    assert neutered != src
    with tempfile.NamedTemporaryFile(
        'w', suffix='.js', dir=HERE, delete=False, encoding='utf-8'
    ) as fh:
        scratch = fh.name
        fh.write(neutered)
    try:
        output = _run_raw(scratch)
    finally:
        try:
            os.remove(scratch)
        except OSError:
            pass
    assert 'FAIL single_status_zone_after_3_frames' in output, (
        'NEUTER ineffective: without the write-back the staircase did NOT '
        'return — the guard would pass on the buggy code too:\n' + output)
    assert 'FAIL single_status_zone_fresh_bubble' in output, (
        'NEUTER ineffective on the fresh-bubble arm:\n' + output)


if __name__ == '__main__':
    for fn in (test_status_zone_is_singleton_across_frames,
               test_NEUTER_writeback_is_load_bearing):
        try:
            fn()
            print('  PASS', fn.__name__)
        except Exception as e:  # noqa: BLE001
            print('  RED ', fn.__name__, '::', str(e)[:400])

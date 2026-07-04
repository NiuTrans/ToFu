"""Regression: buried (mid-list) empty-ghost assistant placeholders must be
swept on load, not just the tail.

WHY
---
``_classifyGhostTail`` only ever inspected ``conv.messages[length-1]``. Once a
NEWER turn was appended on top of an empty placeholder (from an aborted /
failed / empty-stop turn the user retried), that placeholder was no longer the
tail, so nothing removed it — repeated failed attempts STACKED blank "Agent"
bubbles mid-transcript. Real DB case ``mr3jfcw10pianj`` accumulated 4 buried
empties (idx 79/80/81/84) and the chat read as cluttered + "can't continue".

``_sweepBuriedGhostAssistants(conv)`` (static/js/main/main_init_tasks.js)
removes every buried empty placeholder while LEAVING the tail for
``_classifyGhostTail`` to reconcile. ``_isBuriedEmptyGhost(msg)`` is the pure
predicate: an assistant turn with no content, no thinking fragment, no error,
and no REAL tool round — removed even when it carries a settled
finishReason/usage (mid-list it renders as a body-less badge-only bubble =
clutter). Special turns (endpoint / autopilot VU / image-gen) are never swept.

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
eval(fs.readFileSync(process.argv[2], 'utf8'));  // main_init_tasks.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

if (typeof _isBuriedEmptyGhost !== 'function' || typeof _sweepBuriedGhostAssistants !== 'function') {
  console.log('FAIL fns_exposed _isBuriedEmptyGhost/_sweepBuriedGhostAssistants missing');
  process.exit(0);
}
check('fns_exposed', true);

// ── _isBuriedEmptyGhost predicate ──
// Truly empty placeholder (no finishReason) → buried ghost.
check('empty_no_finish_is_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', thinking: '' }) === true);
// Empty but ABORTED with usage (conv mr3jfcw10pianj idx 79) → still buried ghost
// (mid-list it's a body-less badge-only bubble).
check('empty_aborted_with_usage_is_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', finishReason: 'aborted',
    usage: { input_tokens: 5 } }) === true);
// Real content → NOT a ghost.
check('has_content_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: 'hello' }) === false);
// Thinking fragment → keep (renders a thinking block, not a blank).
check('thinking_only_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', thinking: 'I' }) === false);
// Error envelope → keep (renders an error block).
check('error_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', error: { kind: 'internal' } }) === false);
// Real tool round → keep.
check('real_round_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '',
    toolRounds: [{ status: 'done', toolName: 'run_command' }] }) === false);
// Empty/searching round (no real work) → still a ghost.
check('empty_round_is_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '',
    toolRounds: [{ status: 'searching' }] }) === true);
// Special turns are never swept.
check('endpoint_planner_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', _isEndpointPlanner: true }) === false);
check('vu_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', _isVirtualUser: true }) === false);
check('imagegen_not_ghost',
  _isBuriedEmptyGhost({ role: 'assistant', content: '', _igResult: { image_url: 'x' } }) === false);
// Non-assistant / nullish.
check('user_not_ghost', _isBuriedEmptyGhost({ role: 'user', content: '' }) === false);
check('null_not_ghost', _isBuriedEmptyGhost(null) === false);

// ── _sweepBuriedGhostAssistants — the mr3jfcw10pianj shape ──
// user, [3 buried empties], real assistant, user, buried empty, tail assistant
const conv = { id: 'mr3jfcw10pianj', messages: [
  { role: 'user', content: 'Q' },                                             // 0
  { role: 'assistant', content: '', finishReason: 'aborted', usage: {} },     // 1 buried ghost
  { role: 'assistant', content: '' },                                         // 2 buried ghost
  { role: 'assistant', content: '' },                                         // 3 buried ghost
  { role: 'assistant', content: 'real reply', finishReason: 'stop' },         // 4 keep
  { role: 'user', content: 'Q2' },                                            // 5 keep
  { role: 'assistant', content: '' },                                         // 6 buried ghost
  { role: 'assistant', content: 'tail', finishReason: 'stop' },               // 7 tail keep
]};
const removed = _sweepBuriedGhostAssistants(conv);
check('swept_count_4', removed === 4);
check('after_len_4', conv.messages.length === 4);
check('after_seq',
  conv.messages.map(m => m.role + ':' + (m.content || '_')).join('|')
    === 'user:Q|assistant:real reply|user:Q2|assistant:tail');

// ── Tail is a buried-ghost shape but MUST be left for _classifyGhostTail ──
const convTailGhost = { id: 't', messages: [
  { role: 'user', content: 'Q' },
  { role: 'assistant', content: '', finishReason: 'aborted' },  // tail — leave alone
]};
const removedT = _sweepBuriedGhostAssistants(convTailGhost);
check('tail_ghost_left_alone', removedT === 0 && convTailGhost.messages.length === 2);

// ── Idempotent: a second sweep removes nothing ──
const conv2 = { id: 'x', messages: [
  { role: 'user', content: 'Q' },
  { role: 'assistant', content: '' },
  { role: 'assistant', content: 'reply', finishReason: 'stop' },
]};
_sweepBuriedGhostAssistants(conv2);
const removed2 = _sweepBuriedGhostAssistants(conv2);
check('idempotent', removed2 === 0);

// ── Short conv (< 2 msgs) → no-op ──
check('too_short_noop', _sweepBuriedGhostAssistants({ id: 's', messages: [
  { role: 'assistant', content: '' }] }) === 0);

console.log(out.join('\n'));
"""


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_buried_ghost_sweep():
    harness = os.path.join(HERE, '_buried_ghost_harness.js')
    with open(harness, 'w') as f:
        f.write(_HARNESS)
    try:
        proc = subprocess.run(
            ['node', harness, os.path.join(JS_DIR, 'main', 'main_init_tasks.js')],
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
    assert not fails, 'Buried-ghost sweep failures:\n' + output
    assert output.count('PASS') >= 19, f'expected >=19 PASS lines, got:\n{output}'

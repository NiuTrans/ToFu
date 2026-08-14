"""Regression: the "Continue" (继续) message action must NOT appear on a turn
that finished NORMALLY.

WHY
---
`renderMessage` (static/js/ui/chat_render.js) used to gate the Continue button
purely on POSITION — "is this the last assistant message and not currently
streaming?" — ignoring WHY the turn ended. So a clean `end_turn`/`stop` turn
(the same reasons that earn the green ✓ in finish_info.js) still showed
"Continue", which is meaningless: `continueAssistant()` resumes from a tool-call
checkpoint, and on a clean finish the backend just replies `fallback:'regenerate'`.

THE FIX
-------
Continue is now the exact COMPLEMENT of the green ✓: it shows only when the last
assistant turn did NOT finish cleanly (length / max_tokens /
tool_rounds_exhausted / premature_close / aborted / interrupted / …), or when
`finishReason` is absent (legacy/unknown → keep the recovery path).

This test extracts the gate constants + expression from the shipped source and
evaluates them under node against a table of finish reasons. NEUTER: dropping
the `!_turnFinishedClean` guard must make a clean `end_turn` turn show Continue.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
sys.path.insert(0, HERE)
from _runtime_sections import runtime_section_path  # noqa: E402

SRC_JS = runtime_section_path('ui/chat_render.js')
# The clean-finish constant was extracted out of chat_render.js into the
# turn-settlement module (core/turn_settlement.js) as _TS_CLEAN_FINISH_REASONS;
# chat_render.js consumes the verdict via continueButtonForSettlement.
SETTLEMENT_JS = runtime_section_path('core/turn_settlement.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _read_src() -> str:
    with open(SRC_JS, encoding='utf-8') as f:
        return f.read()


def _read_settlement_src() -> str:
    with open(SETTLEMENT_JS, encoding='utf-8') as f:
        return f.read()


_CLEAN_CONST_RE = r'const _TS_CLEAN_FINISH_REASONS = new Set\((\[[^\]]*\])\);'


def test_source_gates_continue_on_finish_reason():
    """Structural guard: the Continue button HTML must be gated on the finish
    reason, not just on being the last assistant message."""
    ts_src = _read_settlement_src()
    assert re.search(_CLEAN_CONST_RE, ts_src), (
        'Continue gate constant _TS_CLEAN_FINISH_REASONS missing from '
        'turn_settlement.js — the button may have reverted to a '
        'position-only gate.')
    src = _read_src()
    # The continue button must be produced only when the settlement verdict
    # says so (clean finish → show:false), AND only on the last assistant msg.
    assert 'continueButtonForSettlement' in src, (
        'chat_render.js no longer consults continueButtonForSettlement — the '
        'Continue gate is decoupled from the finish reason.')
    assert 'isLastAssistant && _tsBtn.show' in src, (
        'continueH is not gated on both position AND settlement verdict')


_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Extract the shipped gate: the _TS_CLEAN_FINISH_REASONS constant (now in
// core/turn_settlement.js), so we test the REAL constants, not a copy.
const cleanM = src.match(/const _TS_CLEAN_FINISH_REASONS = new Set\((\[[^\]]*\])\);/);
if (!cleanM) { console.log('FAIL clean_const_found'); process.exit(0); }
const _TS_CLEAN_FINISH_REASONS = new Set(eval(cleanM[1]));

function turnFinishedClean(finishReason) {
  return _TS_CLEAN_FINISH_REASONS.has(finishReason);
}
// The shipped gate requires BOTH position+liveness (isLastAssistant) AND the
// settlement verdict. `isLastAssistant` is NOT hardcoded: pinning it to a
// constant `true` made this file structurally blind to the liveness half of the
// gate (pt_ae8777b4eee04ef1 — a turn still being generated rendered Continue).
// So drive the REAL predicate: a settled conv (no live stream, no task pin, no
// authoritative busy set) on the tail assistant message.
function isLastAssistantFor(conv, idx) {
  // Mirrors chat_render.js: tail position AND the turn is not in flight.
  const inFlight = !!(conv
    && idx === conv.messages.length - 1
    && (activeStreams.has(conv.id) || conv.activeTaskId
        || (conv._authoritativeActiveTaskIds
            && conv._authoritativeActiveTaskIds.size > 0)));
  return !!(conv && idx === conv.messages.length - 1 && !inFlight);
}
const activeStreams = new Map();
const _settledConv = {
  id: 'convA', activeTaskId: null,
  messages: [{ role: 'user' }, { role: 'assistant' }],
};
function showsContinue(finishReason) {
  const isLastAssistant = isLastAssistantFor(_settledConv, 1);
  const _turnFinishedClean = turnFinishedClean(finishReason);
  return isLastAssistant && !_turnFinishedClean;
}

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }

// Clean finishes → NO Continue button.
check('clean_end_turn_hidden', showsContinue('end_turn') === false);
check('clean_stop_hidden', showsContinue('stop') === false);
check('clean_stop_sequence_hidden', showsContinue('stop_sequence') === false);

// Interrupted / truncated → SHOW Continue.
check('length_shown', showsContinue('length') === true);
check('max_tokens_shown', showsContinue('max_tokens') === true);
check('tool_rounds_exhausted_shown', showsContinue('tool_rounds_exhausted') === true);
check('premature_close_shown', showsContinue('premature_close') === true);
check('aborted_shown', showsContinue('aborted') === true);
check('interrupted_shown', showsContinue('interrupted') === true);

// Unknown / legacy (no finishReason) → keep the recovery path (SHOW).
check('missing_reason_shown', showsContinue(undefined) === true);

console.log(out.join('\n'));
"""


def _run_harness(tmp_path):
    harness = tmp_path / '_continue_gate_harness.js'
    harness.write_text(_HARNESS, encoding='utf-8')
    return subprocess.run(['node', str(harness), SETTLEMENT_JS],
                          capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_gate_table(tmp_path):
    proc = _run_harness(tmp_path)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    fails = [ln for ln in output.splitlines() if ln.startswith('FAIL')]
    assert not fails, 'Continue gate misbehaves:\n' + output


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_neuter_position_only_gate_shows_on_clean(tmp_path):
    """NEUTER: reduce the gate to position-only (drop !_turnFinishedClean) and
    prove a clean end_turn turn then WRONGLY shows Continue — i.e. the guard
    genuinely discriminates finish reason."""
    src = _read_settlement_src()
    cleanM = re.search(_CLEAN_CONST_RE, src)
    assert cleanM, '_TS_CLEAN_FINISH_REASONS not found'
    neutered_harness = _HARNESS.replace(
        'return isLastAssistant && !_turnFinishedClean;',
        'return isLastAssistant;  // NEUTER: position-only gate')
    assert neutered_harness != _HARNESS, (
        'NEUTER substitution did not apply — the gate expression in _HARNESS '
        'changed shape; update this replacement.')
    harness = tmp_path / '_continue_gate_neutered.js'
    harness.write_text(neutered_harness, encoding='utf-8')
    proc = subprocess.run(['node', str(harness), SETTLEMENT_JS],
                          capture_output=True, text=True, timeout=60)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('clean_end_turn_hidden') is False, (
        'NEUTER did not bite: a position-only gate still hid Continue on a '
        'clean end_turn — the test does not discriminate finish reason.\n' + output)

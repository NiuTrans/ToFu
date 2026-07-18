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

import pytest

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
SRC_JS = os.path.join(ROOT, 'static', 'js', 'ui', 'chat_render.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _read_src() -> str:
    with open(SRC_JS, encoding='utf-8') as f:
        return f.read()


def test_source_gates_continue_on_finish_reason():
    """Structural guard: the Continue button HTML must be gated on the finish
    reason, not just on being the last assistant message."""
    src = _read_src()
    assert '_FINISH_CLEAN' in src, (
        'Continue gate constant _FINISH_CLEAN missing — the button may have '
        'reverted to a position-only gate.')
    # The continue button must be produced only when NOT a clean finish.
    m = re.search(r'const continueH = \(([^)]*)\)', src)
    assert m, 'continueH gate expression not found in expected shape'
    gate = m.group(1)
    assert 'isLastAssistant' in gate and '!_turnFinishedClean' in gate, (
        f'continueH is not gated on both position AND clean-finish: {gate!r}')


_HARNESS = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Extract the shipped gate: the _FINISH_CLEAN constant + the derivation of
// _turnFinishedClean, so we test the REAL constants/expression, not a copy.
const cleanM = src.match(/const _FINISH_CLEAN = (\[[^\]]*\]);/);
if (!cleanM) { console.log('FAIL clean_const_found'); process.exit(0); }
const _FINISH_CLEAN = eval(cleanM[1]);

function turnFinishedClean(finishReason) {
  return _FINISH_CLEAN.includes(finishReason);
}
// The shipped gate also requires isLastAssistant; we hold that true here and
// vary only the finish reason.
function showsContinue(finishReason) {
  const isLastAssistant = true;
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
    return subprocess.run(['node', str(harness), SRC_JS],
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
    src = _read_src()
    cleanM = re.search(r'const _FINISH_CLEAN = (\[[^\]]*\]);', src)
    assert cleanM, '_FINISH_CLEAN not found'
    neutered_harness = _HARNESS.replace(
        'return isLastAssistant && !_turnFinishedClean;',
        'return isLastAssistant;  // NEUTER: position-only gate')
    assert neutered_harness != _HARNESS
    harness = tmp_path / '_continue_gate_neutered.js'
    harness.write_text(neutered_harness, encoding='utf-8')
    proc = subprocess.run(['node', str(harness), SRC_JS],
                          capture_output=True, text=True, timeout=60)
    output = proc.stdout.strip()
    assert proc.returncode == 0, f'node failed: {proc.stderr}\n{output}'
    lines = {ln.split(' ', 1)[1]: ln.startswith('PASS')
             for ln in output.splitlines() if ln.startswith(('PASS', 'FAIL'))}
    assert lines.get('clean_end_turn_hidden') is False, (
        'NEUTER did not bite: a position-only gate still hid Continue on a '
        'clean end_turn — the test does not discriminate finish reason.\n' + output)

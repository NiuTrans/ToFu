"""Terminal error tag on a COMPLETED turn's failed flow step (mtgrjqtuhzi4i9).

A goal-mode/flow worker step can fail (LLM dispatch 400) inside a turn that
later completes. Its durable message carries ``msg.error`` — before this fix
``renderFinishInfo`` looked only at ``_turnStatus``, so the failed worker
bubble got a bare ✓ over its "[LLM error at round 3] No substantive answer"
placeholder and the real rejection never reached the UI. Now any truthy
``msg.error`` renders the error tag (with the plain-string detail) instead
of the ✓, while a clean completed turn keeps it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._runtime_sections import runtime_section


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not shutil.which('node'), reason='node not installed')
def test_msg_error_on_completed_turn_renders_error_tag_not_check(tmp_path):
    finish_owner = runtime_section('ui/finish_info.js')
    fn_start = finish_owner.index('function _quotaPct(value) {')
    fn_end_marker = (
        '  if (parts.length === 0) return "";\n'
        '  return `<div class="message-finish">${parts.join("")}</div>`;\n'
        '}'
    )
    fn_end = finish_owner.index(fn_end_marker, fn_start) + len(fn_end_marker)
    source_path = tmp_path / 'finish-terminal.js'
    source_path.write_text(finish_owner[fn_start:fn_end], encoding='utf-8')

    harness = tmp_path / 'finish-terminal-harness.js'
    harness.write_text(r"""
const fs = require('fs');
global.window = global;
global.runtimeScope = global;
global.escapeHtml = (value) => String(value ?? '')
  .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
global.t = (key, params) => {
  let value = key;
  for (const [name, replacement] of Object.entries(params || {}))
    value = value.replaceAll('{' + name + '}', String(replacement));
  return value;
};
global.Icon = () => '';
global.calcCostCny = () => ({ costCny: 0 });
// finishPresentation → null: exercise the hardcoded fallbacks.
global.ConversationTurnStore = { finishPresentation: () => null };
// normalizeErrorEnvelope / errorEnvelopeMessage / errorEnvelopeKindLabel are
// deliberately NOT defined: the typeof guards fall through to the
// plain-string detail path under test.

eval(fs.readFileSync(process.argv[2], 'utf8'));

const ERR = 'Bad request (HTTP 400): conflicting keywords found in anyOf with parent';
const base = { _turnSettlement: {}, model: '', usage: {} };
console.log(JSON.stringify({
  clean_completed: renderFinishInfo(
    { ...base, _turnStatus: 'completed' }, false),
  errored_completed: renderFinishInfo(
    { ...base, _turnStatus: 'completed', error: ERR }, false),
  errored_failed: renderFinishInfo(
    { ...base, _turnStatus: 'failed', error: 'boom' }, false),
  plain_failed: renderFinishInfo(
    { ...base, _turnStatus: 'failed' }, false),
}));
""", encoding='utf-8')

    run = subprocess.run(
        [shutil.which('node'), str(harness), str(source_path)],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr
    rendered = json.loads(run.stdout.strip().splitlines()[-1])

    clean = rendered['clean_completed']
    assert 'finish-tag ok' in clean
    assert 'finish-tag err' not in clean

    errored = rendered['errored_completed']
    assert 'finish-tag err' in errored, errored
    assert 'finish-tag ok' not in errored
    assert 'conflicting keywords found in anyOf with parent' in errored

    failed = rendered['errored_failed']
    assert 'finish-tag err' in failed
    assert 'boom' in failed

    # The pre-fix failed-turn path is unchanged: generic label, no detail.
    plain = rendered['plain_failed']
    assert 'finish-tag err' in plain
    assert 'finish-tag ok' not in plain

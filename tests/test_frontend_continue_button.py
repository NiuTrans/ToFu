#!/usr/bin/env python3
"""tests/test_frontend_continue_button.py — pin the honest Continue-button
affordance driven by the turn-settlement verdict.

Epic pt_a4484f3ad3134ea8. The chat_render Continue-button gate used to show
"Continue" for ANY non-clean finishReason (aborted / error / missing alike)
without checking whether a recoverable checkpoint exists — the "button says
Continue, actually regenerates" lie (it silently fell back to a full
regeneration). The gate now consumes the verdict via
``continueButtonForSettlement(computeTurnSettlement(msg, model))`` and labels
honestly per ``resume.mode``:

  * none       (clean finish)            → button hidden
  * prefill    (lossless resume)         → "Continue"   (kind=continue, lossless)
  * checkpoint (resume from round N)     → "Continue"   (kind=continue, lossy)
  * regenerate (no honest resume)        → "Regenerate" (kind=regenerate) — the fix

This drives the REAL shipped static/js/core/turn_settlement.js in Node (NOT a
re-implementation) and asserts the affordance for a corpus of messages.

Run standalone:  python3 tests/test_frontend_continue_button.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_PATH = os.path.join(ROOT, 'static', 'js', 'core', 'turn_settlement.js')

CAPABLE = 'gpt-4o'
INCAPABLE = 'claude-sonnet-4-5'


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _amsg(content='', thinking='', finish_reason=None, tool_rounds=None, role='assistant'):
    m = {'role': role, 'content': content, 'thinking': thinking,
         'toolRounds': tool_rounds or []}
    if finish_reason is not None:
        m['finishReason'] = finish_reason
    return m


def _done_round(call_id, llm_round=0):
    return {'toolCallId': call_id, 'toolName': 'read_files', 'status': 'done',
            'toolContent': 'res', 'llmRound': llm_round, 'assistantContent': 'p'}


def _drive_button(cases):
    """Run continueButtonForSettlement(computeTurnSettlement(...)) in Node."""
    corpus_path = os.path.join(HERE, '_tsbtn_corpus.json')
    driver_path = os.path.join(HERE, '_tsbtn_driver.js')
    with open(corpus_path, 'w') as f:
        json.dump(cases, f)
    driver = r"""
const fs = require('fs');
global.window = global;
const src = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(src);
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map(c =>
  continueButtonForSettlement(computeTurnSettlement(c.msg, c.model, null)));
console.log(JSON.stringify(out));
"""
    with open(driver_path, 'w') as f:
        f.write(driver)
    try:
        proc = subprocess.run(['node', driver_path, JS_PATH, corpus_path],
                              capture_output=True, text=True, timeout=30)
    finally:
        for p in (corpus_path, driver_path):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node driver failed: {proc.stderr}'
    return json.loads(proc.stdout.strip())


def _run(cases):
    return _drive_button(cases)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_completed_turn_hides_button():
    cases = [
        {'msg': _amsg(content='a', finish_reason='stop'), 'model': CAPABLE},
        {'msg': _amsg(content='a', finish_reason='end_turn'), 'model': CAPABLE},
        {'msg': _amsg(content='a', finish_reason='stop_sequence'), 'model': CAPABLE},
    ]
    out = _run(cases)
    assert all(o['show'] is False for o in out), f'completed turn must hide the button: {out}'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_prefill_shows_lossless_continue():
    cases = [
        {'msg': _amsg(content='the tail', finish_reason='aborted'), 'model': CAPABLE},
        {'msg': _amsg(content='the tail', finish_reason='length'), 'model': CAPABLE},
        {'msg': _amsg(content='the tail', finish_reason='interrupted'), 'model': CAPABLE},
    ]
    out = _run(cases)
    for o in out:
        assert o['show'] is True
        assert o['kind'] == 'continue'
        assert o['lossless'] is True
        assert o['labelKey'] == 'msgAction.continue'
        assert o['titleKey'] == 'msgAction.continueLosslessTitle'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_checkpoint_shows_lossy_continue_with_kept_rounds():
    """The checkpoint face is now the FALLBACK: a tools turn the provider can't
    prefill (Claude) → Continue from round N (lossy). A CAPABLE tools turn now
    yields prefill (see test_capable_tools_turn_prefers_lossless_prefill)."""
    cases = [{'msg': _amsg(content='partial', finish_reason='interrupted',
                           tool_rounds=[_done_round('c1', 0), _done_round('c2', 1)]),
              'model': INCAPABLE}]
    out = _run(cases)
    o = out[0]
    assert o['show'] is True
    assert o['kind'] == 'continue'
    assert o['lossless'] is False
    assert o['keptRounds'] == 2
    assert o['titleKey'] == 'msgAction.continueFromRoundTitle'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_capable_tools_turn_prefers_lossless_prefill():
    """THE P5 FLIP (owner-approved): a capable model with a resumable tail on a
    TOOLS turn shows the LOSSLESS 'Continue' affordance (prefill), matching the
    case-2 wire the route already ships — the button is now honest that the
    resume is lossless (keptRounds is surfaced for the tool replay, but the
    mode is prefill)."""
    cases = [{'msg': _amsg(content='partial', finish_reason='interrupted',
                           tool_rounds=[_done_round('c1', 0), _done_round('c2', 1)]),
              'model': CAPABLE}]
    out = _run(cases)
    o = out[0]
    assert o['show'] is True
    assert o['kind'] == 'continue'
    assert o['lossless'] is True
    assert o['keptRounds'] == 2
    assert o['titleKey'] == 'msgAction.continueLosslessTitle'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_regenerate_shows_honest_regen_label():
    """The honesty fix: a turn with NO honest resume is labelled Regenerate,
    not Continue. Covers: error (failed), claude-length (prefill declined),
    empty turn, and a missing finishReason (not in the resumable set)."""
    cases = [
        {'msg': _amsg(content='partial', finish_reason='error'), 'model': CAPABLE},
        {'msg': _amsg(content='partial', finish_reason='length'), 'model': INCAPABLE},
        {'msg': _amsg(finish_reason='aborted'), 'model': CAPABLE},          # empty turn
        {'msg': _amsg(content='partial'), 'model': CAPABLE},                 # missing fr
    ]
    out = _run(cases)
    for o in out:
        assert o['show'] is True, f'resume affordance must remain (honest): {out}'
        assert o['kind'] == 'regenerate', f'must be honestly labelled regenerate: {out}'
        assert o['labelKey'] == 'msgAction.regen'
        assert o['titleKey'] == 'msgAction.regenerateTitle'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_non_assistant_or_null_hides_button():
    cases = [
        {'msg': _amsg(content='hi', role='user'), 'model': CAPABLE},   # computeTurnSettlement → null
    ]
    out = _run(cases)
    assert out[0]['show'] is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

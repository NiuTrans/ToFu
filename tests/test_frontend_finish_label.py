#!/usr/bin/env python3
"""tests/test_frontend_finish_label.py — pin the verdict-driven interrupt
bubble finish-tag.

Epic pt_a4484f3ad3134ea8. The finish_info.js finish-reason tag used to
re-derive its label from msg.finishReason independently (a THIRD inference
alongside the Continue button and the resume decision). It now reads the
single settlement verdict via
``finishLabelForSettlement(computeTurnSettlement(msg, null), finishReason)``
→ a label ``kind``, which the renderer maps to the SAME labels/styling as
before (byte-identical output — the change is architectural, not cosmetic).

This drives the REAL shipped static/js/core/turn_settlement.js in Node (NOT a
re-implementation) and asserts the label kind for the full finishReason
vocabulary, including the faithful 3-way interrupted family (killed / restart
/ unknown — the verdict's CAUSE_UNKNOWN keeps an absent interruptedReason
honest instead of over-committing it to restart) and the 'fallback' pass-
through for reasons the verdict deliberately does not classify
(tool_use / tool_calls / a future reason).

Run standalone:  python3 tests/test_frontend_finish_label.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

from tests._runtime_sections import runtime_section_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
JS_PATH = runtime_section_path('core/turn_settlement.js')


def _node_available() -> bool:
    return bool(shutil.which('node'))


def _drive(cases):
    """Run finishLabelForSettlement(computeTurnSettlement(msg, null), fr)."""
    corpus_path = os.path.join(HERE, '_tslbl_corpus.json')
    driver_path = os.path.join(HERE, '_tslbl_driver.js')
    with open(corpus_path, 'w') as f:
        json.dump(cases, f)
    driver = r"""
const fs = require('fs');
global.window = global;
const src = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(src);
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = cases.map(c =>
  finishLabelForSettlement(computeTurnSettlement(c.msg, null, null), c.fr));
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


def _msg(fr, interrupted_reason=None):
    m = {'role': 'assistant', 'content': 'partial answer', 'thinking': '',
         'toolRounds': [], 'finishReason': fr}
    if interrupted_reason is not None:
        m['interruptedReason'] = interrupted_reason
    return m


# (finishReason, interruptedReason, expected label kind)
CORPUS = [
    ('stop', None, 'ok'),
    ('end_turn', None, 'ok'),
    ('stop_sequence', None, 'ok'),
    ('error', None, 'error'),
    ('abnormal_stop', None, 'abnormal'),
    ('aborted', None, 'stopped'),
    ('interrupted', 'killed', 'interruptedKilled'),
    ('interrupted', 'manual', 'interruptedRestart'),
    ('interrupted', None, 'interruptedUnknown'),   # absent → honest "unknown", NOT restart
    ('server_offline', None, 'serverOffline'),
    ('premature_close', None, 'gateway'),
    ('incomplete', None, 'incomplete'),
    ('length', None, 'truncated'),
    ('max_tokens', None, 'truncated'),
    ('tool_rounds_exhausted', None, 'toolLimit'),
    ('content_filter', None, 'filtered'),
    # reasons the verdict deliberately does NOT classify → legacy fallback
    ('tool_use', None, 'fallback'),
    ('tool_calls', None, 'fallback'),
    ('some_future_reason', None, 'fallback'),
]


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_finish_label_kind_for_full_vocabulary():
    cases = [{'msg': _msg(fr, ir), 'fr': fr} for (fr, ir, _k) in CORPUS]
    out = _drive(cases)
    assert len(out) == len(CORPUS)
    mismatches = []
    for (fr, ir, want), got in zip(CORPUS, out):
        if got.get('kind') != want:
            mismatches.append(f'  fr={fr} ir={ir}: want {want}, got {got.get("kind")}')
    assert not mismatches, 'finish-label kind divergence:\n' + '\n'.join(mismatches)


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_absent_interrupted_reason_is_unknown_not_restart():
    """The CAUSE_UNKNOWN refinement: a legacy/first_boot interrupted turn (no
    interruptedReason) must be honestly 'unknown', NOT over-committed to
    'restart'. This is the faithful 3-way the bubble already showed."""
    out = _drive([{'msg': _msg('interrupted', None), 'fr': 'interrupted'}])
    assert out[0]['kind'] == 'interruptedUnknown'
    assert out[0]['kind'] != 'interruptedRestart'


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_error_vs_abnormal_stop_disambiguated_by_raw_reason():
    """Both map to CAUSE_ERROR in the verdict, but the bubble distinguishes
    them by the raw finishReason (preserving the reasonError vs
    abnormalInterrupt labels)."""
    out = _drive([
        {'msg': _msg('error'), 'fr': 'error'},
        {'msg': _msg('abnormal_stop'), 'fr': 'abnormal_stop'},
    ])
    assert out[0]['kind'] == 'error'
    assert out[1]['kind'] == 'abnormal'


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

#!/usr/bin/env python3
"""tests/test_frontend_turn_settlement_equivalence.py — lock the JS port to
the Python SSOT (ghost-tail / conv_state_reducer precedent).

Epic pt_a4484f3ad3134ea8 (design docs/TURN_SETTLEMENT.md). The backend
``lib/conversations/turn_settlement.py::compute_turn_settlement`` is the
single source of truth for the per-turn settlement verdict. The frontend port
``static/js/core/turn_settlement.js::computeTurnSettlement`` lets the client
render the SAME verdict on a cold reopen / streamed message without a server
round-trip. They MUST agree byte-for-byte or the interrupt bubble / Continue
button would show one thing while the backend's Continue route does another —
the exact divergence this epic exists to kill.

This suite drives BOTH implementations over ONE shared corpus of
(message, model) inputs and asserts the verdicts are deep-equal:

  * Python verdict computed in-process (the SSOT).
  * JS verdict computed by loading the REAL shipped
    static/js/core/turn_settlement.js into a Node subprocess (NOT a
    re-implementation) and calling computeTurnSettlement on the same corpus.

Corpus coverage: every finishReason in the vocabulary (clean / truncated /
failed / interrupted variants incl. the interruptedReason→cause mapping /
missing / unknown), × content presence, × the checkpoint-scan edge cases
(status-break, toolContent reconstruction from results, llmRound vs roundNum
batching, no-toolCallId), × capable (gpt) vs fail-closed (claude) model,
× the manual-Stop ('aborted') lossless case.

Run standalone:
    python3 tests/test_frontend_turn_settlement_equivalence.py
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


# ─────────────────────────────────────────────────────────────────────────
#  Corpus
# ─────────────────────────────────────────────────────────────────────────

def _amsg(content='', thinking='', finish_reason=None, tool_rounds=None,
          interrupted_reason=None):
    m = {'role': 'assistant', 'content': content, 'thinking': thinking,
         'toolRounds': tool_rounds or []}
    if finish_reason is not None:
        m['finishReason'] = finish_reason
    if interrupted_reason is not None:
        m['interruptedReason'] = interrupted_reason
    return m


def _done_round(call_id, name='read_files', content='res', llm_round=None,
                round_num=None):
    r = {'toolCallId': call_id, 'toolName': name, 'status': 'done',
         'toolContent': content, 'assistantContent': 'prose'}
    if llm_round is not None:
        r['llmRound'] = llm_round
    if round_num is not None:
        r['roundNum'] = round_num
    return r


def _corpus():
    """One shared list of {name, msg, model} exercised by BOTH implementations."""
    cases = []

    def add(name, msg, model=CAPABLE):
        cases.append({'name': name, 'msg': msg, 'model': model})

    # ── outcome classification across the full finishReason vocabulary ──
    for fr in ('stop', 'end_turn', 'stop_sequence'):
        add(f'clean_{fr}', _amsg(content='answer', finish_reason=fr))
    for fr in ('length', 'max_tokens'):
        add(f'truncated_{fr}', _amsg(content='partial', finish_reason=fr))
    add('tool_cap', _amsg(content='x', finish_reason='tool_rounds_exhausted'))
    add('safety_cap', _amsg(content='x', finish_reason='incomplete'))
    add('content_filter', _amsg(content='x', finish_reason='content_filter'))
    for fr in ('error', 'abnormal_stop'):
        add(f'failed_{fr}', _amsg(content='x', finish_reason=fr))
    add('interrupted_killed', _amsg(content='x', finish_reason='interrupted',
                                    interrupted_reason='killed'))
    add('interrupted_manual_restart', _amsg(content='x', finish_reason='interrupted',
                                            interrupted_reason='manual'))
    add('interrupted_unknown_restart', _amsg(content='x', finish_reason='interrupted'))
    add('server_offline', _amsg(content='x', finish_reason='server_offline'))
    add('premature_close', _amsg(content='x', finish_reason='premature_close'))
    add('aborted_manual', _amsg(content='x', finish_reason='aborted'))
    add('missing_finish_reason', _amsg(content='x'))
    add('unknown_finish_reason', _amsg(content='x', finish_reason='some_future_reason'))

    # ── resume: empty turn / completed-with-tools / checkpoint ──
    add('empty_interrupted', _amsg(finish_reason='interrupted'))
    add('completed_with_tools', _amsg(
        content='full', finish_reason='stop',
        tool_rounds=[_done_round('c1', llm_round=0)]))
    add('checkpoint_two_done', _amsg(
        content='partial', finish_reason='interrupted',
        tool_rounds=[_done_round('c1', llm_round=0), _done_round('c2', llm_round=1)]))
    add('checkpoint_status_break', _amsg(
        content='partial', finish_reason='interrupted',
        tool_rounds=[_done_round('c1', llm_round=0),
                     {'toolCallId': 'c2', 'toolName': 'grep', 'status': 'running'}]))
    add('checkpoint_toolcontent_reconstruct', _amsg(
        content='partial', finish_reason='interrupted',
        tool_rounds=[{'toolCallId': 'c1', 'toolName': 'web_search', 'status': 'done',
                      'toolContent': None,
                      'results': [{'snippet': 's1'}, {'title': 't2'}]}]))
    add('checkpoint_roundnum_fallback_batch', _amsg(
        content='partial', finish_reason='interrupted',
        tool_rounds=[_done_round('c1', round_num=1), _done_round('c2', round_num=3)]))
    add('checkpoint_failed_outcome_still_resumes', _amsg(
        content='', thinking='', finish_reason='error',
        tool_rounds=[_done_round('c1', llm_round=0)]))
    add('no_toolcallid_rounds_regenerate', _amsg(
        content='partial', finish_reason='interrupted',
        tool_rounds=[{'toolName': 'orphan', 'status': 'done', 'toolContent': 'x'}]))

    # ── resume: prefill (capable) vs fail-closed (claude) vs regenerate ──
    add('prefill_length_capable', _amsg(content='the tail', finish_reason='length'))
    add('prefill_interrupted_capable', _amsg(content='the tail', finish_reason='interrupted'))
    add('prefill_aborted_capable', _amsg(content='the tail', finish_reason='aborted'))
    add('prefill_length_claude_declined', _amsg(content='the tail', finish_reason='length'),
        model=INCAPABLE)
    add('aborted_claude_declined', _amsg(content='the tail', finish_reason='aborted'),
        model=INCAPABLE)
    add('prefill_model_none_declined', _amsg(content='the tail', finish_reason='length'),
        model=None)
    add('clean_stop_not_resumed_even_with_content', _amsg(content='done', finish_reason='stop'))
    add('error_no_checkpoint_regenerate', _amsg(content='partial', finish_reason='error'))
    add('missing_fr_no_checkpoint_regenerate', _amsg(content='partial'))

    # ── thinking-only turn (non-empty via thinking) ──
    add('thinking_only_prefill', _amsg(thinking='reasoning', finish_reason='interrupted'))

    return cases


# ─────────────────────────────────────────────────────────────────────────
#  Both halves
# ─────────────────────────────────────────────────────────────────────────

def _python_verdicts(corpus):
    from lib.conversations.turn_settlement import compute_turn_settlement
    return [compute_turn_settlement(c['msg'], model=c['model']) for c in corpus]


def _js_verdicts(corpus):
    """Drive the real shipped turn_settlement.js in Node; return verdicts."""
    corpus_path = os.path.join(HERE, '_ts_equiv_corpus.json')
    driver_path = os.path.join(HERE, '_ts_equiv_driver.js')
    with open(corpus_path, 'w') as f:
        json.dump(corpus, f)
    driver = r"""
const fs = require('fs');
global.window = global;                 // so the module's window-publish fires
const src = fs.readFileSync(process.argv[2], 'utf8');
(0, eval)(src);
const corpus = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = corpus.map(c => computeTurnSettlement(c.msg, c.model, null));
console.log(JSON.stringify(out));
"""
    with open(driver_path, 'w') as f:
        f.write(driver)
    try:
        proc = subprocess.run(
            ['node', driver_path, JS_PATH, corpus_path],
            capture_output=True, text=True, timeout=30,
        )
    finally:
        for p in (corpus_path, driver_path):
            try:
                os.remove(p)
            except OSError:
                pass
    assert proc.returncode == 0, f'node verdict driver failed: {proc.stderr}'
    return json.loads(proc.stdout.strip())


def _norm(v):
    """Normalize a verdict (either impl) for deep comparison."""
    if v is None:
        return None
    r = v['resume']
    return {
        'outcome': v['outcome'],
        'finishReason': v['finishReason'],
        'cause': v['cause'],
        'resume': {
            'mode': r['mode'],
            'lossless': bool(r['lossless']),
            'keptRounds': int(r['keptRounds']),
            'prefillChars': int(r['prefillChars']),
            'reason': r['reason'],
        },
    }


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_backend_frontend_verdict_equivalence():
    corpus = _corpus()
    py = [_norm(v) for v in _python_verdicts(corpus)]
    js = [_norm(v) for v in _js_verdicts(corpus)]
    assert len(py) == len(js) == len(corpus)
    mismatches = []
    for case, pv, jv in zip(corpus, py, js):
        if pv != jv:
            mismatches.append(
                f"  [{case['name']}] model={case['model']}\n"
                f"    py={json.dumps(pv, sort_keys=True)}\n"
                f"    js={json.dumps(jv, sort_keys=True)}")
    assert not mismatches, (
        f'{len(mismatches)}/{len(corpus)} verdict(s) diverge between the Python '
        f'SSOT and the JS port:\n' + '\n'.join(mismatches))


@pytest.mark.skipif(not _node_available(), reason='node not installed')
def test_js_port_loads_and_exposes_compute_turn_settlement():
    """Sanity ratchet: the shipped module loads in Node and exposes the entry
    point (guards against a bundler/publish regression making the equivalence
    test trivially green by both halves failing identically)."""
    verdicts = _js_verdicts([
        {'name': 'x', 'msg': _amsg(content='a tail', finish_reason='aborted'),
         'model': CAPABLE},
    ])
    assert verdicts[0] is not None, 'JS port returned null for an assistant message'
    assert verdicts[0]['resume']['mode'] == 'prefill'
    assert verdicts[0]['resume']['lossless'] is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

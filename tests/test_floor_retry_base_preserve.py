#!/usr/bin/env python3
"""FloorRetry adoption must PRESERVE the prior rounds' prose (base-preserve).

THE LATENT BUG (owner audit on pt_6e12b1ffd95a453e, 2026-07-24)
---------------------------------------------------------------
``stream_llm_response``'s FloorRetry convergence used to do a WHOLESALE
replace::

    task['content']  = msg.get('content') or ''
    task['thinking'] = msg.get('reasoning_content') or ''

The adopted ``msg`` holds ONLY the current round's text (the resend re-
generated this round from an identical body). But ``task['content']`` /
``task['thinking']`` ACCUMULATE across the whole turn — the main orchestrator
loop has no per-round content reset (only the one-time contentPrefix seed at
``_run.py:501``), so round R3's accumulators are
``R1 preamble + R2 prose + R3 first-attempt tail``. A wholesale replace on a
FloorRetry adoption in R3 therefore DELETED R1+R2 from the task state — the
preamble the model already delivered (and the user already read) vanished
from the persisted answer.

This is the same residue class as the transport-retry duplication fixed in
34471811, on the opposite axis: that fix stops abandoned-attempt text from
STACKING; this fix stops the adoption convergence from DROPPING prior rounds.

The fix keeps the round base captured at stream entry and replaces only this
round's tail::

    task['content']  = _round_base_content  + msg.content
    task['thinking'] = _round_base_thinking + msg.reasoning_content

Residue recording still snapshots the FULL pre-convergence text (base
included) because the checkpointed conv row mirrors that full text and the
terminal-guard exemption byte-matches on it.

Failing-first / NEUTER discipline
---------------------------------
* ``test_floor_retry_adoption_preserves_prior_rounds`` is RED on the
  pre-fix wholesale replace (content collapses to just the adopted text).
* ``test_floor_retry_residue_still_records_full_snapshot`` pins that the
  residue entry keeps the FULL accumulated text (base + draft) — the
  byte-match contract the terminal guard exemption relies on.
* NEUTER (manual, verified 2026-07-24): reverting the convergence to the
  wholesale replace flips exactly the first test red; the second stays green
  (residue was always full-snapshot).

Run directly (env-guarded):
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python tests/test_floor_retry_base_preserve.py
"""
from __future__ import annotations

import os
import sys
import threading as _thr

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import quart as _quart  # noqa: E402
sys.modules.setdefault('flask', _quart)

pytestmark = pytest.mark.unit


_FLOOR_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 28654,
                'cache_creation_input_tokens': 42000, '_wire_fp': [{'k': 'a'}]}
_HIT_USAGE = {'prompt_tokens': 10, 'cache_read_tokens': 150000,
              'cache_creation_input_tokens': 1200, '_wire_fp': [{'k': 'a'}]}


def _seed_wire_fp(conv_id, fp):
    from lib.tasks_pkg.cache_tracking._state import (
        _cache_lock,
        _cache_states,
    )
    from lib.tasks_pkg.cache_tracking._state import CacheState, _state_key
    key = _state_key(conv_id, user_id=1)
    with _cache_lock:
        st = _cache_states.get(key)
        if st is None:
            st = CacheState()
            _cache_states[key] = st
        st.wire_fp = list(fp)


def _run_stream_with_script(task, monkeypatch, dispatch_seq, *, enabled=True):
    """Drive the REAL stream_llm_response with a scripted dispatch sequence.
    Mirrors the harness in tests/test_floor_retry_residue.py: the primary
    attempt (seq[0]) may stream deltas; resends stream nothing (production
    Layer-1 discipline: on_content=None)."""
    import lib.tasks_pkg.manager._stream as _mgr
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY', '1' if enabled else '0')
    monkeypatch.setenv('TOFU_CACHE_FLOOR_RETRY_MAX', '2')
    calls = {'n': 0}
    seq = list(dispatch_seq)

    def _fake_dispatch(body, **kwargs):
        i = calls['n']
        calls['n'] += 1
        item = seq[min(i, len(seq) - 1)]
        oc = kwargs.get('on_content')
        if oc and item.get('stream'):
            for chunk in item['stream']:
                oc(chunk)
        ot = kwargs.get('on_thinking')
        if ot and item.get('stream_thinking'):
            for chunk in item['stream_thinking']:
                ot(chunk)
        return ({'role': 'assistant',
                 'content': item.get('final', ''),
                 'reasoning_content': item.get('thinking_final', '')},
                item.get('finish', 'stop'), dict(item['usage']))

    _orig = _mgr.dispatch_stream
    _mgr.dispatch_stream = _fake_dispatch
    try:
        return _mgr.stream_llm_response(
            task, {'model': 'aws.claude-opus-4.8',
                   'messages': [{'role': 'system', 'content': 'S'},
                                {'role': 'user', 'content': 'go'}]},
            tag='R3')
    finally:
        _mgr.dispatch_stream = _orig


def _mk_accumulated_task(conv_id, content, thinking):
    """A task mid-turn: content/thinking already hold PRIOR rounds' prose
    (the main orchestrator never resets them between rounds)."""
    return {'id': f'task-bp-{conv_id}', 'convId': conv_id, '_userId': 1,
            'content': content, 'thinking': thinking,
            'config': {}, 'events': [], 'toolRounds': [],
            'content_lock': _thr.Lock(), 'events_lock': _thr.Lock()}


def test_floor_retry_adoption_preserves_prior_rounds(monkeypatch):
    """FAILING-FIRST: a FloorRetry adoption in R3 must keep R1+R2 prose in
    the task accumulators — only the CURRENT round's tail is replaced by the
    adopted resend text. RED on the pre-fix wholesale replace (the
    accumulators collapse to just the adopted text)."""
    conv_id = 'bp-preserve'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    base_c = 'R1 preamble prose. R2 tool narration. '
    base_t = 'R1 reasoning. R2 reasoning. '
    task = _mk_accumulated_task(conv_id, base_c, base_t)
    draft = 'R3-first-attempt-draft'
    adopted = 'R3-adopted-final'
    _run_stream_with_script(task, monkeypatch, [
        {'stream': [draft], 'stream_thinking': ['r3-draft-think'],
         'final': draft, 'thinking_final': 'r3-draft-think',
         'usage': _FLOOR_USAGE},
        {'final': adopted, 'thinking_final': 'r3-final-think',
         'usage': _HIT_USAGE},
    ])
    assert task['content'] == base_c + adopted, (
        'adoption must replace ONLY the current round\'s tail — prior '
        f'rounds\' prose must survive; got content={task["content"]!r}')
    assert task['thinking'] == base_t + 'r3-final-think', (
        f'thinking axis must preserve prior rounds too; got {task["thinking"]!r}')


def test_floor_retry_residue_still_records_full_snapshot(monkeypatch):
    """The residue entry must keep the FULL pre-convergence text (base +
    first-attempt draft): the checkpointed conv row mirrors that full text
    and the terminal-guard exemption byte-matches on it. Trimming the base
    out of the residue would silently break the exemption."""
    conv_id = 'bp-residue'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    base_c = 'prior rounds. '
    task = _mk_accumulated_task(conv_id, base_c, 'prior think. ')
    draft = 'draft-tail'
    _run_stream_with_script(task, monkeypatch, [
        {'stream': [draft], 'final': draft, 'usage': _FLOOR_USAGE},
        {'final': 'adopted', 'usage': _HIT_USAGE},
    ])
    residue = task.get('_floor_retry_residue') or []
    assert len(residue) == 1, f'exactly one discarded attempt; got {residue}'
    assert residue[0]['content'] == base_c + draft, (
        'residue must byte-match the FULL accumulated text (what the conv '
        f'row mirrored); got {residue[0]["content"]!r}')


def test_floor_retry_empty_base_behaves_like_wholesale(monkeypatch):
    """No-regression pin for the single-round case the existing residue suite
    covers: with an empty round base, base-preserve == wholesale replace
    (content converges to exactly the adopted text)."""
    conv_id = 'bp-empty'
    _seed_wire_fp(conv_id, [{'k': 'a'}])
    task = _mk_accumulated_task(conv_id, '', '')
    _run_stream_with_script(task, monkeypatch, [
        {'stream': ['draft'], 'final': 'draft', 'usage': _FLOOR_USAGE},
        {'final': 'adopted-final', 'usage': _HIT_USAGE},
    ])
    assert task['content'] == 'adopted-final'


if __name__ == '__main__':
    sys.exit(pytest.main([__file__, '-v', '-p', 'no:cacheprovider']))

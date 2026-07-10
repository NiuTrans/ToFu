#!/usr/bin/env python3
"""Headless tests for STREAMING describe-to-recommend.

The recommend flow was converted from a single blocking call (spinner → dump)
into a server-owned task that streams grounded arXiv cards one at a time,
aligned with the Q&A tab's poll transport. This suite proves the stream is
**real** — grounded cards are emitted incrementally as each candidate resolves,
BEFORE the terminal ``done`` event — and is not a decorative reveal over an
all-at-once payload.

The engine imports ``dispatch_stream`` / ``search_arxiv`` / ``fetch_arxiv_title``
at module scope, so we monkeypatch them ON THE MODULE to run fully offline
(same fake-arXiv seam as ``test_paper_recommend_grounding``). The interpretation
agent's fake ``dispatch_stream`` returns the final JSON in one no-tool round.

DOUBLE-NEUTER (``test_neuter_confirms_streaming_is_load_bearing``): asserts the
mid-stream invariant against the REAL generator (a card is observable before
``done``), then NEUTERS the per-candidate emit (buffer candidates and yield
them lumped only at ``done`` — the pre-streaming behaviour), and asserts the
invariant now BREAKS (no card is observable until ``done``), then RESTORES.

Run standalone: ``python3 tests/test_paper_recommend_stream.py``
"""

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('TRADING_ENABLED', '0')

import lib.paper.recommend_engine as re_mod  # noqa: E402
import lib.paper.recommend_task as task_mod  # noqa: E402
from lib.paper.recommend_runtime import (  # noqa: E402
    _new_recommend_task,
    _recommend_runtime,
)


def _color(s, c): return f'\033[{c}m{s}\033[0m'
def _ok(msg): print(' ', _color('✓', '32'), msg)
def _fail(msg): print(' ', _color('✗', '31'), msg); sys.exit(1)


_FAKE_ARXIV = {
    '2502.09992': {
        'arxiv_id': '2502.09992', 'title': 'Large Language Diffusion Models',
        'authors': ['Shen Nie'], 'summary': 'LLaDA.', 'published': '2025-02-14',
        'primary_category': 'cs.CL', 'pdf_url': '', 'abs_url': '',
    },
    '2504.12216': {
        'arxiv_id': '2504.12216',
        'title': 'd1: Scaling Reasoning in Diffusion Large Language Models via Reinforcement Learning',
        'authors': ['Siyan Zhao'], 'summary': 'diffu-GRPO.', 'published': '2025-04-16',
        'primary_category': 'cs.CL', 'pdf_url': '', 'abs_url': '',
    },
    '2505.17638': {
        'arxiv_id': '2505.17638',
        'title': "Why Diffusion Models Don't Memorize: The Role of Implicit Dynamical Regularization in Training",
        'authors': ['Tony Bonnaire'], 'summary': 'memorization.', 'published': '2025-05-23',
        'primary_category': 'cs.LG', 'pdf_url': '', 'abs_url': '',
    },
}


def _fake_search_by_title(title):
    tl = (title or '').lower()
    if 'large language diffusion' in tl or 'llada' in tl:
        return [_FAKE_ARXIV['2502.09992']]
    if tl.startswith('d1') or ('scaling reasoning' in tl and 'diffusion' in tl):
        return [_FAKE_ARXIV['2504.12216']]
    if "don't memorize" in tl or 'implicit dynamical' in tl:
        return [_FAKE_ARXIV['2505.17638']]
    return []


class _Patched:
    """Install fake LLM + arXiv seam; optionally SLOW grounding so a background
    worker's incremental event log can be observed mid-flight."""
    def __init__(self, llm_reply, *, ground_delay=0.0):
        self.llm_reply = llm_reply
        self.ground_delay = ground_delay
        self._orig = {}

    def __enter__(self):
        self._orig['dispatch_stream'] = re_mod.dispatch_stream
        self._orig['search_arxiv'] = re_mod.search_arxiv
        self._orig['fetch_arxiv_title'] = re_mod.fetch_arxiv_title
        reply = self.llm_reply
        delay = self.ground_delay

        def _fake_dispatch_stream(messages, *, on_content=None, tools=None, **kw):
            body = reply if isinstance(reply, str) else json.dumps(reply)
            if on_content:
                on_content(body)
            return ({'role': 'assistant', 'content': body, 'tool_calls': None},
                    'stop', {'prompt_tokens': 1, 'completion_tokens': 1})

        def _fake_search(query, max_results=10):
            if delay:
                time.sleep(delay)
            return _fake_search_by_title(query)[:max_results]

        def _fake_fetch(arxiv_id):
            return ''

        re_mod.dispatch_stream = _fake_dispatch_stream
        re_mod.search_arxiv = _fake_search
        re_mod.fetch_arxiv_title = _fake_fetch
        return self

    def __exit__(self, *exc):
        for k, v in self._orig.items():
            setattr(re_mod, k, v)
        return False


# Two real dLLM papers → two grounded candidate events, plus a correction.
_REPLY = {
    'candidates': [
        {'title': 'Large Language Diffusion Models', 'arxiv_id': '2502.09992',
         'venue': 'NeurIPS 2025 Oral', 'why': 'flagship dLLM.'},
        {'title': 'd1: Scaling Reasoning in Diffusion Large Language Models via Reinforcement Learning',
         'arxiv_id': None, 'venue': 'NeurIPS 2025', 'why': 'RL for dLLMs.'},
    ],
    'correction': {
        'note': 'No dLLM won a NeurIPS award; the diffusion Best Paper was about memorization.',
        'paper': {'title': "Why Diffusion Models Don't Memorize", 'arxiv_id': '2505.17638'},
    },
}


def test_generator_emits_interpret_then_candidates_then_done():
    """The generator surfaces the two-phase pipeline as ordered events."""
    with _Patched(_REPLY):
        evs = list(re_mod.iter_recommend_events('diffusion LM award papers', 6))
    types = [e['type'] for e in evs]
    assert types[0] == 'interpret_done', f'first event must be interpret_done: {types}'
    assert types[-1] == 'done', f'last event must be done: {types}'
    # interpret_done reports how many grounding attempts (for skeleton count).
    assert evs[0]['candidateCount'] == 2, f'candidateCount wrong: {evs[0]}'
    assert evs[0]['correctionPending'] is True
    cand_evs = [e for e in evs if e['type'] == 'candidate']
    assert len(cand_evs) == 2, f'expected 2 candidate events: {types}'
    assert [c['index'] for c in cand_evs] == [0, 1], 'candidate indices not sequential'
    ids = [c['card']['arxiv_id'].split('v')[0] for c in cand_evs]
    assert ids == ['2502.09992', '2504.12216'], f'grounded ids/order wrong: {ids}'
    # A candidate event MUST precede the correction and the done event.
    assert types.index('candidate') < types.index('correction') < types.index('done')
    corr = [e for e in evs if e['type'] == 'correction'][0]
    assert corr['correction']['paper']['arxiv_id'].split('v')[0] == '2505.17638'
    assert evs[-1]['resultCount'] == 2 and evs[-1]['correctionPresent'] is True
    _ok('generator emits interpret_done → candidate(×2) → correction → done, in order')


def test_candidate_carries_full_grounded_card():
    with _Patched(_REPLY):
        evs = list(re_mod.iter_recommend_events('dLLM papers', 6))
    card = [e for e in evs if e['type'] == 'candidate'][0]['card']
    assert card['title'] == 'Large Language Diffusion Models', 'real arXiv title missing'
    assert card['authors'], 'authors not populated from arXiv metadata'
    assert card['why'] == 'flagship dLLM.' and card['venue'] == 'NeurIPS 2025 Oral'
    _ok('each candidate event carries a fully grounded card (real title/authors + why/venue)')


def test_blocking_wrapper_matches_stream():
    """The legacy blocking wrapper is a faithful drain of the generator."""
    with _Patched(_REPLY):
        out = re_mod.recommend_papers('dLLM award papers', 6)
    ids = [c['arxiv_id'].split('v')[0] for c in out['results']]
    assert ids == ['2502.09992', '2504.12216'], f'wrapper result mismatch: {ids}'
    assert out['correction']['paper']['arxiv_id'].split('v')[0] == '2505.17638'
    assert out['llmError'] is False
    _ok('blocking recommend_papers() wrapper drains the generator to the same aggregate')


def _run_task_and_snapshot_midstream(reply, ground_delay=0.25):
    """Spawn the real background worker with SLOW grounding; poll the task's
    append-only event log and record, for the FIRST poll that sees the initial
    grounded candidate, whether the task had already reached 'done'.

    Returns (saw_candidate_before_done, final_types).
    """
    task_id = f'rectest_{int(time.time() * 1000)}'
    task = _new_recommend_task(task_id, 'diffusion LM award papers', 6)
    saw_candidate_before_done = {'v': None}

    def _poll():
        while True:
            t = _recommend_runtime.get(task_id)
            if not t:
                return
            with t['events_lock']:
                types = [e.get('type') for e in t['events']]
            has_cand = 'candidate' in types
            is_done = ('done' in types) or t['status'] in ('done', 'error')
            if has_cand and saw_candidate_before_done['v'] is None:
                # Record the relationship at the moment the first card appears.
                saw_candidate_before_done['v'] = not is_done
            if is_done:
                return
            time.sleep(0.02)

    poller = threading.Thread(target=_poll, daemon=True)
    with _Patched(reply, ground_delay=ground_delay):
        poller.start()
        task_mod._run_recommend_task(task)   # runs to completion inline
        poller.join(timeout=5)

    t = _recommend_runtime.get(task_id)
    with t['events_lock']:
        final_types = [e.get('type') for e in t['events']]
    return saw_candidate_before_done['v'], final_types


def test_task_log_reveals_candidates_before_done():
    """A mid-stream poll of the task log sees a grounded card BEFORE 'done'."""
    saw_before_done, final_types = _run_task_and_snapshot_midstream(_REPLY)
    assert 'candidate' in final_types and 'done' in final_types, \
        f'task did not stream candidate+done: {final_types}'
    assert saw_before_done is True, \
        'the first grounded card only became visible at/after done — the stream is not real'
    _ok('task event log exposes grounded cards incrementally, before the done event')


def test_neuter_confirms_streaming_is_load_bearing():
    """DOUBLE-NEUTER: prove the per-candidate emit is what makes it stream.

    1. REAL generator → a card is observable in the task log BEFORE done.
    2. NEUTER: monkeypatch iter_recommend_events to a NON-streaming variant that
       buffers candidates and yields them ALL only at done (the pre-conversion
       behaviour) → no card is observable before done (invariant BREAKS).
    3. RESTORE → invariant holds again.
    """
    # 1. Real generator streams.
    saw_before_done, _ = _run_task_and_snapshot_midstream(_REPLY)
    assert saw_before_done is True, 'real generator did not stream (precondition failed)'

    # 2. Neuter the emit: same grounding, but candidate events are withheld
    #    until the very end (lumped just before done) — a fake "stream".
    orig_iter = re_mod.iter_recommend_events

    def _non_streaming_iter(description, max_results=6, *, abort=None, on_tool_event=None):
        buffered = []
        for ev in orig_iter(description, max_results, abort=abort, on_tool_event=on_tool_event):
            if ev['type'] == 'candidate':
                buffered.append(ev)          # withhold — do NOT yield yet
                continue
            if ev['type'] == 'done':
                for b in buffered:           # dump them all at the end
                    yield b
                yield ev
                continue
            yield ev

    # The task worker imports iter_recommend_events into task_mod's namespace.
    task_mod.iter_recommend_events = _non_streaming_iter
    try:
        saw_before_done_neutered, final_types = _run_task_and_snapshot_midstream(_REPLY)
    finally:
        task_mod.iter_recommend_events = orig_iter

    assert 'candidate' in final_types, f'neutered variant lost cards entirely: {final_types}'
    assert saw_before_done_neutered is False, \
        'NEUTER did not break the invariant — cards still appeared before done, so the ' \
        'test is not actually exercising the per-candidate stream (false-confidence test).'

    # 3. Restore confirmed.
    saw_before_done_restored, _ = _run_task_and_snapshot_midstream(_REPLY)
    assert saw_before_done_restored is True, 'streaming not restored after neuter'
    _ok('DOUBLE-NEUTER: per-candidate emit is load-bearing (neuter lumps at done, restore streams)')


def main():
    print()
    print(_color('═══ Paper Recommend Streaming Tests ═══', '36'))
    print()
    tests = [
        test_generator_emits_interpret_then_candidates_then_done,
        test_candidate_carries_full_grounded_card,
        test_blocking_wrapper_matches_stream,
        test_task_log_reveals_candidates_before_done,
        test_neuter_confirms_streaming_is_load_bearing,
    ]
    for fn in tests:
        try:
            fn()
        except AssertionError as e:
            _fail(f'{fn.__name__}: {e}')
        except Exception as e:
            import traceback
            traceback.print_exc()
            _fail(f'{fn.__name__}: unexpected {type(e).__name__}: {e}')
    print()
    print(_color(f'═══ ALL {len(tests)} TESTS PASSED ═══', '32'))
    print()


if __name__ == '__main__':
    main()

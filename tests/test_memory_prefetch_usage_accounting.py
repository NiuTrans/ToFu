"""tests/test_memory_prefetch_usage_accounting.py — the per-turn memory-prefetch
rerank is a real billed LLM call and must appear on the bill.

THE GAP
=======
``run_memory_prefetch`` fires a cheap-model rerank on EVERY user turn (default
on). Its usage landed only in ``diag['usage']``; no caller folded it into
``task['usage']``, so the request was invisible to the cost popover, the wallet
and the daily report.

The timeout path is worse. ``_run_with_deadline`` enforces the 800 ms budget by
ABANDONING a daemon worker — it stops WAITING, it does not stop the REQUEST. The
gateway still processes it and still charges for it; the result is simply
dropped. So the rounds that cost us something and returned nothing were exactly
the ones that recorded nothing. Same class as the FloorRetry and turn-retry
accounting bugs: a real billed request hidden from the report.

THE FIX
=======
Thread a ``usage_sink`` callback down to the dispatch and invoke it from INSIDE
the worker thread, so it fires whether the caller waited or abandoned. The
orchestrator seam binds a sink that folds the usage into the same
``_checkpointUsage`` carry-forward slot ``_finalize_task`` already merges into
the terminal usage.

Layering kept clean: ``lib/memory/prefetch`` knows nothing about task dicts, it
just calls the sink it was handed. The additive arithmetic itself lives once, in
``lib.cost.merge_usage_totals``.

Run:
    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_memory_prefetch_usage_accounting.py -v
"""
from __future__ import annotations

import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cost import merge_usage_totals  # noqa: E402
from lib.memory.prefetch import _rerank  # noqa: E402

pytestmark = pytest.mark.unit


# ── The pure arithmetic (single source, shared with the retry path) ─────

def test_merge_usage_totals_is_additive():
    a = {'prompt_tokens': 100, 'completion_tokens': 20}
    b = {'prompt_tokens': 50, 'completion_tokens': 5, 'cache_read_tokens': 900}
    assert merge_usage_totals(a, b) == {
        'prompt_tokens': 150, 'completion_tokens': 25, 'cache_read_tokens': 900}


def test_merge_usage_totals_skips_non_numeric_and_bools():
    """Real usage dicts carry trace_id / _dispatch / nested detail dicts."""
    out = merge_usage_totals(
        {'prompt_tokens': 10},
        {'prompt_tokens': 5, 'trace_id': 'abc', '_dispatch': {'key': 'k1'},
         'stream': True})
    assert out == {'prompt_tokens': 15}


def test_merge_usage_totals_does_not_mutate_its_inputs():
    a = {'prompt_tokens': 1}
    b = {'prompt_tokens': 2}
    merge_usage_totals(a, b)
    assert a == {'prompt_tokens': 1} and b == {'prompt_tokens': 2}


def test_merge_usage_totals_tolerates_none():
    assert merge_usage_totals(None, {'prompt_tokens': 3}) == {'prompt_tokens': 3}
    assert merge_usage_totals({'prompt_tokens': 3}, None) == {'prompt_tokens': 3}
    assert merge_usage_totals(None, None) == {}


# ── The reranker reports its usage ──────────────────────────────────────

def _memories(n=6):
    return [{'name': 'm%d' % i, 'description': 'd%d' % i, 'body': 'b%d' % i,
             'tags': []} for i in range(n)]


def _patch_dispatch(monkeypatch, *, usage, delay=0.0, content='{"ids": [1]}'):
    """Patch dispatch_chat at its import site inside _do_dispatch."""
    import lib.llm_dispatch as ld

    def _fake(*a, **kw):
        if delay:
            time.sleep(delay)
        return content, dict(usage)

    monkeypatch.setattr(ld, 'dispatch_chat', _fake)


def test_rerank_reports_usage_on_the_happy_path(monkeypatch):
    seen = []
    _patch_dispatch(monkeypatch, usage={'prompt_tokens': 1234,
                                        'completion_tokens': 56})

    selected, diag = _rerank._call_cheap_reranker(
        _memories(), [0, 1, 2, 3], 'recent', current_request='req',
        usage_sink=seen.append)

    assert seen, 'the rerank made a billed call but reported no usage'
    assert seen[0]['prompt_tokens'] == 1234
    assert diag.get('usage', {}).get('prompt_tokens') == 1234


def test_rerank_reports_usage_EVEN_WHEN_THE_DEADLINE_ABANDONS_IT(monkeypatch):
    """THE CORE CASE: the deadline stops us WAITING, not the gateway BILLING.

    The abandoned daemon worker completes moments later; its usage must still
    reach the sink, because that request was charged for.
    """
    import lib.memory.prefetch as facade
    monkeypatch.setattr(facade, 'PREFETCH_DEADLINE_MS', 40)

    reported = threading.Event()
    seen = []

    def _sink(u):
        seen.append(u)
        reported.set()

    _patch_dispatch(monkeypatch, usage={'prompt_tokens': 999}, delay=0.35)

    selected, diag = _rerank._call_cheap_reranker(
        _memories(), [0, 1, 2, 3], 'recent', current_request='req',
        usage_sink=_sink)

    # The caller gave up and injected nothing — unchanged contract.
    assert selected == []
    assert diag.get('timed_out') is True

    # ...but the abandoned worker's bill must still arrive.
    assert reported.wait(3.0), (
        'the abandoned rerank was billed by the gateway but never reported '
        'its usage — this is the silent spend')
    assert seen[0]['prompt_tokens'] == 999


def test_fast_skip_makes_no_call_and_reports_nothing(monkeypatch):
    """Below PREFETCH_MIN_CANDIDATES no dispatch happens — nothing to bill."""
    seen = []

    def _boom(*a, **kw):
        raise AssertionError('dispatch must not run on the fast-skip path')

    import lib.llm_dispatch as ld
    monkeypatch.setattr(ld, 'dispatch_chat', _boom)

    selected, diag = _rerank._call_cheap_reranker(
        _memories(), [0], 'recent', usage_sink=seen.append)
    assert diag.get('skipped') == 'too_few_candidates'
    assert seen == []


def test_a_raising_dispatch_reports_nothing_and_still_propagates(monkeypatch):
    """No-fallback policy preserved; a failed call has no usage to record."""
    import lib.llm_dispatch as ld
    seen = []

    def _raise(*a, **kw):
        raise RuntimeError('cheap model down')

    monkeypatch.setattr(ld, 'dispatch_chat', _raise)

    with pytest.raises(RuntimeError):
        _rerank._call_cheap_reranker(
            _memories(), [0, 1, 2, 3], 'recent', usage_sink=seen.append)
    assert seen == []


def test_sink_is_optional(monkeypatch):
    """Callers that do not care (scripts, tests) must still work."""
    _patch_dispatch(monkeypatch, usage={'prompt_tokens': 7})
    selected, diag = _rerank._call_cheap_reranker(
        _memories(), [0, 1, 2, 3], 'recent')
    assert diag.get('usage', {}).get('prompt_tokens') == 7


def test_a_throwing_sink_never_breaks_the_turn(monkeypatch):
    """Accounting is advisory — it must not take the turn down with it."""
    _patch_dispatch(monkeypatch, usage={'prompt_tokens': 7})

    def _bad(_u):
        raise RuntimeError('accounting exploded')

    selected, diag = _rerank._call_cheap_reranker(
        _memories(), [0, 1, 2, 3], 'recent', usage_sink=_bad)
    assert diag.get('usage', {}).get('prompt_tokens') == 7


# ── The orchestrator seam folds it onto the task ────────────────────────

def _task(**extra):
    t = {'id': 'memtask01', 'convId': 'convmem', 'status': 'running'}
    t.update(extra)
    return t


def test_orchestrator_sink_folds_usage_into_the_carry_forward_slot():
    from lib.tasks_pkg.orchestrator._memory_prefetch import make_prefetch_usage_sink
    task = _task()
    sink = make_prefetch_usage_sink(task)
    sink({'prompt_tokens': 400, 'completion_tokens': 30})

    carried = task.get('_checkpointUsage') or {}
    assert carried.get('prompt_tokens') == 400
    assert carried.get('completion_tokens') == 30


def test_orchestrator_sink_accumulates_and_composes():
    """Two prefetches (or a prefetch on top of a resumed checkpoint) add up."""
    from lib.tasks_pkg.orchestrator._memory_prefetch import make_prefetch_usage_sink
    task = _task(_checkpointUsage={'prompt_tokens': 1000})
    sink = make_prefetch_usage_sink(task)
    sink({'prompt_tokens': 400})
    sink({'prompt_tokens': 25})
    assert (task['_checkpointUsage'] or {}).get('prompt_tokens') == 1425


def test_orchestrator_sink_is_thread_safe():
    """The sink fires from an abandoned worker thread, possibly concurrently
    with the turn's own bookkeeping."""
    from lib.tasks_pkg.orchestrator._memory_prefetch import make_prefetch_usage_sink
    task = _task()
    sink = make_prefetch_usage_sink(task)

    def _hammer():
        for _ in range(50):
            sink({'prompt_tokens': 1})

    threads = [threading.Thread(target=_hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert (task['_checkpointUsage'] or {}).get('prompt_tokens') == 200


def test_late_arrival_after_the_task_settled_is_surfaced_not_swallowed():
    """A worker that lands AFTER finalize cannot be folded into the bill any
    more — it must be logged as an audit metric rather than dropped silently,
    so the spend is still discoverable."""
    from lib.tasks_pkg.orchestrator import _memory_prefetch as mp
    recorded = []
    orig = mp.audit_log
    mp.audit_log = lambda ev, **kw: recorded.append((ev, kw))
    try:
        task = _task(status='done')
        mp.make_prefetch_usage_sink(task)({'prompt_tokens': 321})
    finally:
        mp.audit_log = orig

    assert not (task.get('_checkpointUsage') or {}), (
        'a settled task must not be re-billed behind the finalizer'
    )
    assert recorded and recorded[0][0] == 'memory_prefetch_usage_orphaned', (
        'late usage must be surfaced as an audit metric, got %s' % recorded)
    assert recorded[0][1].get('prompt_tokens') == 321


def test_empty_usage_is_a_no_op():
    from lib.tasks_pkg.orchestrator._memory_prefetch import make_prefetch_usage_sink
    task = _task()
    make_prefetch_usage_sink(task)({})
    make_prefetch_usage_sink(task)(None)
    assert not (task.get('_checkpointUsage') or {})

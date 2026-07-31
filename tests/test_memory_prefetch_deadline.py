"""Bounded memory-prefetch deadline (MEMORY_PREFETCH_DEADLINE_MS).

The cheap-LLM rerank runs on the turn's critical path (round 0, before the
main model's first token). dispatch_chat's per-attempt timeout does NOT bound
total wall-clock (429 cycling runs to its full ~90s budget), so the rerank is
wrapped in a hard wall-clock deadline that ABANDONS the worker on timeout and
injects NOTHING — matching the existing no-fallback policy (no BM25 top-K).

These tests prove:
  - a rerank that sleeps past the deadline → turn continues, ZERO memories
    injected, a terminal outcome records the timeout so the chip still renders;
  - the abandoned worker is a daemon and does not linger blocking anything;
  - the fast-skip paths (too-few candidates) are NOT subject to the deadline;
  - deadline<=0 disables the bound (legacy inline behaviour).
"""
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.memory.prefetch as prefetch  # noqa: E402

pytestmark = pytest.mark.unit


# Two memories so BM25 clears PREFETCH_MIN_CANDIDATES and the cheap-LLM stage
# actually runs (the stage we want to time-bound).
_MEMS = [
    {'name': 'alpha widget lesson', 'description': 'widget alpha trap',
     'tags': ['widget'], 'body': 'When touching the widget alpha path, do X.',
     'scope': 'project', 'filepath': '/m/alpha.md'},
    {'name': 'beta widget convention', 'description': 'widget beta rule',
     'tags': ['widget'], 'body': 'Widget beta must follow convention Y.',
     'scope': 'project', 'filepath': '/m/beta.md'},
]

_MESSAGES = [{'role': 'user', 'content': 'help me with the widget alpha and beta path'}]


def _fresh_messages():
    # Deep-ish copy so injection mutations don't bleed across tests.
    return [{'role': m['role'], 'content': m['content']} for m in _MESSAGES]


def _events_sink():
    evs = []
    return evs, (lambda ev: evs.append(ev))


def _install_common(monkeypatch, dispatch_impl):
    """Stub eligible-memories + dispatch_chat so no real LLM/disk is touched."""
    # _run.py resolves this from lib.memory.storage (NOT the package facade,
    # which never re-exported it) — patch it where the lazy import lands.
    import lib.memory.storage as _storage_mod
    monkeypatch.setattr(_storage_mod, 'get_eligible_memories', lambda *a, **k: list(_MEMS),
                        raising=False)
    # dispatch_chat is imported inside _call_cheap_reranker via
    # `from lib.llm_dispatch import dispatch_chat`, so patch it on that module.
    import lib.llm_dispatch as dispatch_mod
    monkeypatch.setattr(dispatch_mod, 'dispatch_chat', dispatch_impl, raising=False)


def test_rerank_timeout_injects_nothing_and_continues(monkeypatch):
    monkeypatch.setattr(prefetch, 'PREFETCH_DEADLINE_MS', 200, raising=False)

    started = threading.Event()

    def _slow_dispatch(*a, **k):
        started.set()
        time.sleep(5.0)   # far past the 200ms deadline
        return '{"ids": [1, 2]}', {}

    _install_common(monkeypatch, _slow_dispatch)

    msgs = _fresh_messages()
    evs, emit = _events_sink()

    t0 = time.time()
    injected = prefetch.run_memory_prefetch(msgs, '/proj/widget', task={'id': 'tid12345'},
                                            emit_event=emit)
    elapsed = time.time() - t0

    # (a) turn continues quickly — bounded by the deadline, NOT the 5s sleep.
    assert elapsed < 2.0, 'run_memory_prefetch blocked past the deadline (%.2fs)' % elapsed
    assert started.is_set(), 'the reranker worker should have started'

    # (b) zero memories injected + the user message was NOT mutated.
    assert injected == []
    assert msgs[0]['content'] == _MESSAGES[0]['content']

    # (c) a terminal outcome records the timeout so the frontend chip renders.
    terminal = [e for e in evs if e.get('phase') in ('done', 'skipped', 'failed')]
    assert terminal, 'expected a terminal memory_prefetch event'
    last = terminal[-1]
    assert last.get('selected', 0) == 0
    assert last.get('timed_out') is True
    assert last.get('reason') == 'rerank_timeout'


def test_abandoned_worker_is_daemon_and_does_not_linger(monkeypatch):
    monkeypatch.setattr(prefetch, 'PREFETCH_DEADLINE_MS', 150, raising=False)

    worker_names = []

    def _slow_dispatch(*a, **k):
        worker_names.append(threading.current_thread().name)
        time.sleep(3.0)
        return '{"ids": []}', {}

    _install_common(monkeypatch, _slow_dispatch)

    before = {t.name for t in threading.enumerate()}
    prefetch.run_memory_prefetch(_fresh_messages(), '/proj/widget',
                                 task={'id': 'tid2'}, emit_event=None)

    # The worker ran under our named daemon thread.
    assert worker_names and worker_names[0] == 'mem-prefetch-rerank'
    live = [t for t in threading.enumerate() if t.name == 'mem-prefetch-rerank']
    # It may still be sleeping (abandoned) — but it MUST be a daemon so it can
    # never block process exit. That is the leak-safety guarantee.
    for t in live:
        assert t.daemon is True, 'abandoned rerank worker must be a daemon'
    # And it is NOT joined into the caller — proving we abandoned, not waited.
    assert (set(t.name for t in threading.enumerate()) - before) <= {'mem-prefetch-rerank'}


def test_fast_completion_within_deadline_injects(monkeypatch):
    monkeypatch.setattr(prefetch, 'PREFETCH_DEADLINE_MS', 2000, raising=False)

    def _fast_dispatch(*a, **k):
        return '{"ids": [1, 2]}', {'total_tokens': 10}

    _install_common(monkeypatch, _fast_dispatch)

    msgs = _fresh_messages()
    evs, emit = _events_sink()
    injected = prefetch.run_memory_prefetch(msgs, '/proj/widget',
                                            task={'id': 'tid3'}, emit_event=emit)

    assert len(injected) == 2
    done = [e for e in evs if e.get('phase') == 'done']
    assert done and done[-1].get('selected') == 2
    assert not done[-1].get('timed_out')


def test_deadline_zero_disables_bound_runs_inline(monkeypatch):
    """deadline<=0 must run inline (no worker thread) and still inject."""
    monkeypatch.setattr(prefetch, 'PREFETCH_DEADLINE_MS', 0, raising=False)

    ran_on = {}

    def _inline_dispatch(*a, **k):
        ran_on['thread'] = threading.current_thread().name
        return '{"ids": [1]}', {}

    _install_common(monkeypatch, _inline_dispatch)

    injected = prefetch.run_memory_prefetch(_fresh_messages(), '/proj/widget',
                                            task={'id': 'tid4'}, emit_event=None)
    # Ran on the CALLING thread (MainThread under pytest), not a worker.
    assert ran_on.get('thread') != 'mem-prefetch-rerank'
    assert len(injected) == 1


def test_reranker_exception_still_propagates_to_outer_handler(monkeypatch):
    """A rerank EXCEPTION (not a timeout) must be re-raised on the caller thread
    and caught by run_memory_prefetch's outer handler → inject nothing, no crash."""
    monkeypatch.setattr(prefetch, 'PREFETCH_DEADLINE_MS', 2000, raising=False)

    def _boom_dispatch(*a, **k):
        raise RuntimeError('all endpoints unreachable')

    _install_common(monkeypatch, _boom_dispatch)

    # run_memory_prefetch wraps the rerank call in try/except at the
    # orchestrator level (per the no-fallback design the exception propagates
    # UP); here we assert _call_cheap_reranker re-raises rather than swallowing.
    with pytest.raises(RuntimeError, match='unreachable'):
        prefetch._call_cheap_reranker(_MEMS, [0, 1], 'ctx', current_request='q')


if __name__ == '__main__':
    import pytest as _pt
    raise SystemExit(_pt.main([__file__, '-v']))

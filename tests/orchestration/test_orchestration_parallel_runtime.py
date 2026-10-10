"""Direct contracts for the focused orchestration fan-out runtime."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from lib.error_envelope import is_envelope
from lib.orchestration.parallel_runtime import (
    OrchestrationParallelAborted,
    OrchestrationParallelRuntime,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


class _Navigator:
    def find_common_barrier(self, branches):
        return 'join' if branches else None

    def single_next(self, node_id):
        return 'after' if node_id == 'join' else None


class _Outcomes:
    def __init__(self):
        self.failures = []

    def record_node_failure(self, **failure):
        self.failures.append(failure)


class _Abort(Exception):
    pass


def _runtime(branches, walk, *, max_parallel=2, iteration=0):
    events = []
    outcomes = _Outcomes()
    runtime = OrchestrationParallelRuntime(
        navigator=_Navigator(),
        branches=lambda _node_id: list(branches),
        walk=walk,
        outcomes=outcomes,
        emit=events.append,
        max_parallel=max_parallel,
        current_iteration=lambda: iteration,
        abort_errors=(_Abort,),
    )
    return runtime, events, outcomes


def test_parallel_runtime_merges_success_and_structural_failure_honestly():
    def walk(entry, context, *, stop_at):
        assert context == 'seed'
        assert stop_at == 'join'
        if entry == 'broken':
            raise RuntimeError('branch crashed')
        return f'output:{entry}'

    runtime, events, outcomes = _runtime(['good', 'broken'], walk)
    merged, next_node = runtime.run('fanout', 'seed')

    assert next_node == 'after'
    assert 'seed' in merged
    assert 'output:good' in merged
    assert '[branch broken FAILED: RuntimeError: branch crashed]' in merged
    assert outcomes.failures == [{
        'node_id': 'broken',
        'role': None,
        'error': 'RuntimeError: branch crashed',
    }]
    assert events[0] == {
        'type': 'parallel_start',
        'node_id': 'fanout',
        'branches': 2,
    }
    assert events[1]['type'] == 'error'
    assert events[1]['node_id'] == 'broken'
    assert is_envelope(events[1]['error'])
    assert events[1]['error']['kind'] == 'generic'
    assert events[1]['error']['detail'] == \
        'parallel branch failed: branch crashed'


def test_parallel_runtime_translates_abort_without_recording_failure():
    def walk(_entry, _context, *, stop_at):
        assert stop_at == 'join'
        raise _Abort()

    runtime, events, outcomes = _runtime(['one'], walk)

    with pytest.raises(OrchestrationParallelAborted):
        runtime.run('fanout', 'seed')
    assert not outcomes.failures
    assert [event['type'] for event in events] == ['parallel_start']


def test_parallel_runtime_merges_in_graph_order_not_completion_order():
    slow_started = threading.Event()
    release_slow = threading.Event()

    def walk(entry, _context, *, stop_at):
        assert stop_at == 'join'
        if entry == 'slow':
            slow_started.set()
            assert release_slow.wait(timeout=1.0)
        else:
            assert slow_started.wait(timeout=1.0)
            release_slow.set()
        return entry

    runtime, _events, _outcomes = _runtime(['slow', 'fast'], walk)

    merged, _next_node = runtime.run('fanout', 'seed')
    assert merged == 'seed\n\nslow\n\nfast'


def test_parallel_runtime_keeps_empty_fanout_and_engine_boundary_small():
    runtime, events, outcomes = _runtime(
        [], lambda *_args, **_kwargs: pytest.fail('walk must not run'))

    assert runtime.run('fanout', 'seed') == ('seed', None)
    assert events[0]['branches'] == 0
    assert not outcomes.failures

    engine = (ROOT / 'lib' / 'orchestration' / 'engine.py').read_text()
    parallel = (
        ROOT / 'lib' / 'orchestration' / 'parallel_runtime.py').read_text()
    assert 'ThreadPoolExecutor' not in engine
    assert 'as_completed' not in engine
    assert 'return self._parallel_runtime.run(pid, context)' in engine
    assert 'ThreadPoolExecutor' in parallel
    assert 'record_node_failure' in parallel


def test_every_branch_starts_with_idle_reuse_hint_one():
    import time

    branches = [str(i) for i in range(32)]
    barrier = threading.Barrier(len(branches), timeout=10)

    def walk(entry, context, *, stop_at):
        barrier.wait()
        return entry

    runtime, events, outcomes = _runtime(branches, walk, max_parallel=1)
    started = time.monotonic()
    merged, next_node = runtime.run('fanout', 'seed')
    assert merged == 'seed' + ''.join('\n\n' + entry for entry in branches)
    assert next_node == 'after'
    assert not outcomes.failures
    assert events[0]['branches'] == 32
    assert not any(t.name.startswith('flow-par') for t in threading.enumerate())
    print(f'32 branches: {(time.monotonic() - started) * 1000:.2f} ms; 0 workers')


def test_unstarted_branch_failure_keeps_joined_siblings(monkeypatch):
    barrier = threading.Barrier(3, timeout=10)
    original = threading.Thread.start
    starts = 0
    called = []
    lock = threading.Lock()

    def start(thread):
        nonlocal starts
        if thread.name.startswith('flow-par'):
            starts += 1
            if starts == 2:
                raise RuntimeError('injected unstarted branch')
        return original(thread)

    def walk(entry, context, *, stop_at):
        with lock:
            called.append(entry)
        barrier.wait()
        return entry

    monkeypatch.setattr(threading.Thread, 'start', start)
    runtime, events, outcomes = _runtime(['a', 'b', 'c', 'd'], walk, max_parallel=1)
    merged, next_node = runtime.run('fanout', 'seed')
    assert merged == ('seed\n\na\n\n[branch b FAILED: RuntimeError: '
                      'injected unstarted branch]\n\nc\n\nd')
    assert next_node == 'after'
    assert sorted(called) == ['a', 'c', 'd']
    assert len(outcomes.failures) == 1
    assert outcomes.failures[0]['node_id'] == 'b'
    assert [event['node_id'] for event in events if event['type'] == 'error'] == ['b']
    assert not any(t.name.startswith('flow-par') for t in threading.enumerate())


@pytest.mark.parametrize('failure', ['abort', 'event-delivery'])
def test_authoritative_failure_waits_for_published_branch_cleanup(failure):
    from lib.orchestration.graph import FlowEventDeliveryError

    entered, failed, release, finished = (threading.Event() for _ in range(4))
    caught = []

    def walk(entry, context, *, stop_at):
        if entry == 'blocked':
            entered.set()
            assert release.wait(5)
            return entry
        assert entered.wait(5)
        failed.set()
        if failure == 'abort':
            raise _Abort('cancelled')
        raise FlowEventDeliveryError('event not durable')

    runtime, events, outcomes = _runtime(['failed', 'blocked'], walk, max_parallel=1)

    def run():
        try:
            runtime.run('fanout', 'seed')
        except Exception as error:
            caught.append(error)
        finally:
            finished.set()

    caller = threading.Thread(target=run, name='parallel-failure-caller')
    caller.start()
    try:
        assert failed.wait(5)
        assert not finished.wait(0.05), 'published branch must drain before return'
    finally:
        release.set()
        caller.join(5)
    assert not caller.is_alive()
    expected = OrchestrationParallelAborted if failure == 'abort' else FlowEventDeliveryError
    assert len(caught) == 1 and isinstance(caught[0], expected)
    assert not outcomes.failures
    assert [event['type'] for event in events] == ['parallel_start']
    assert not any(t.name.startswith('flow-par') for t in threading.enumerate())

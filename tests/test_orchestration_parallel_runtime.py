"""Direct contracts for the focused orchestration fan-out runtime."""

from __future__ import annotations

from pathlib import Path
import threading

import pytest

from lib.error_envelope import is_envelope
from lib.orchestration_parallel_runtime import (
    OrchestrationParallelAborted,
    OrchestrationParallelRuntime,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


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

    engine = (ROOT / 'lib' / 'orchestration_engine.py').read_text()
    parallel = (
        ROOT / 'lib' / 'orchestration_parallel_runtime.py').read_text()
    assert 'ThreadPoolExecutor' not in engine
    assert 'as_completed' not in engine
    assert 'return self._parallel_runtime.run(pid, context)' in engine
    assert 'ThreadPoolExecutor' in parallel
    assert 'record_node_failure' in parallel

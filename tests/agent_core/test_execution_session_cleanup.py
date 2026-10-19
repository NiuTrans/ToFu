"""Interrupted teardown joins every owned resource and never repeats cleanup."""
import asyncio
import gc
import weakref
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from lib.agent_core.execution_session import ExecutionPhase, ExecutionSession, ResourceDisposition

pytestmark = pytest.mark.unit


def session(name):
    return ExecutionSession(execution_id=name, kind='cleanup-regression', owner_user_id=7)


@pytest.mark.parametrize('error_type', [asyncio.CancelledError, SystemExit])
@pytest.mark.parametrize('recoverable', [False, True])
def test_interrupted_release_finishes_other_resources_and_freezes_failure(error_type, recoverable):
    execution = session(f'interrupted-{error_type.__name__}-{recoverable}')
    calls = []
    error = error_type('cleanup interrupted')
    def interrupted(_):
        calls.append('billing')
        raise error
    execution.hold_resource('billing', interrupted, release_order=3, recoverable=recoverable)
    execution.hold_resource('route', lambda _: calls.append('route'), release_order=2)
    execution.hold_resource('admission', lambda _: calls.append('admission'), release_order=1)
    with pytest.raises(error_type) as caught:
        execution.settle(ExecutionPhase.COMPLETED)
    assert caught.value is error
    assert calls == ['billing', 'route', 'admission']
    assert execution.is_terminal
    receipt = execution.settle(ExecutionPhase.COMPLETED)
    assert receipt.outcome is ExecutionPhase.FAILED
    assert not receipt.invariants_satisfied
    assert dict(receipt.resource_dispositions)['billing'] is (
        ResourceDisposition.DEFERRED if recoverable else ResourceDisposition.FAILED)
    assert execution.snapshot()['held_resources'] == ()
    assert calls == ['billing', 'route', 'admission']


def test_reentrant_settlement_refuses_without_releasing_twice():
    execution = session('reentrant-cleanup')
    calls = []
    def release(_):
        calls.append('route')
        if len(calls) == 1:
            with pytest.raises(RuntimeError, match='settlement.*in progress'):
                execution.settle(ExecutionPhase.FAILED)
    execution.hold_resource('route', release)
    receipt = execution.settle(ExecutionPhase.COMPLETED)
    assert calls == ['route']
    assert receipt.outcome is ExecutionPhase.COMPLETED


def test_parallel_finishers_reuse_interrupted_receipt_without_repeating_callbacks():
    execution = session('parallel-interrupted-cleanup')
    error = asyncio.CancelledError('first release cancelled')
    calls, barrier = [], Barrier(8)
    def release(_):
        calls.append('route')
        raise error
    execution.hold_resource('route', release)
    execution.hold_resource('admission', lambda _: calls.append('admission'), release_order=-1)
    def settle(_):
        barrier.wait(timeout=5)
        try:
            return execution.settle(ExecutionPhase.COMPLETED)
        except asyncio.CancelledError as caught:
            return caught
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(settle, range(8)))
    assert sum(result is error for result in results) == 1
    receipts = [result for result in results if result is not error]
    assert len({id(receipt) for receipt in receipts}) == 1
    assert receipts[0].outcome is ExecutionPhase.FAILED
    assert calls == ['route', 'admission']


@pytest.mark.parametrize('interrupted', [False, True])
def test_terminal_sessions_release_captured_payloads_while_receipts_remain(record_property, interrupted):
    class Payload:
        def __init__(self):
            self.data = bytearray(64 << 10)
    sessions, references, failures = [], [], []
    def interrupt(_):
        raise asyncio.CancelledError('retained cleanup failure')
    for index in range(100):
        execution = session(f'captured-cleanup-{index}')
        payload = Payload()
        references.append(weakref.ref(payload))
        execution.hold_resource('route', lambda _, retained=payload: len(retained.data))
        del payload
        if interrupted:
            execution.hold_resource('billing', interrupt, release_order=1)
            with pytest.raises(asyncio.CancelledError) as caught:
                execution.settle(ExecutionPhase.COMPLETED)
            failures.append(caught.value)
        receipt = execution.settle(ExecutionPhase.COMPLETED)
        assert dict(receipt.resource_dispositions)['route'] is ResourceDisposition.RELEASED
        sessions.append(execution)
    gc.collect()
    retained_bytes = sum(reference() is not None for reference in references) * (64 << 10)
    record_property('captured_payload_bytes', 100 * (64 << 10))
    record_property('retained_payload_bytes_after_settlement', retained_bytes)
    assert retained_bytes == 0
    expected = ExecutionPhase.FAILED if interrupted else ExecutionPhase.COMPLETED
    assert all(execution.settle(ExecutionPhase.FAILED).outcome is expected
               for execution in sessions)


def test_runtime_repair_cannot_publish_success_after_interrupted_cleanup():
    from lib.agent_core.task_runtime import TaskRuntime
    runtime = TaskRuntime('cleanup-runtime')
    task = runtime.create(user_id=7, task_id='interrupted-runtime')
    runtime.mark_running(task['id'])
    calls = []
    def release(_):
        calls.append('route')
        raise asyncio.CancelledError('cleanup cancelled')
    task['_executionSession'].hold_resource('route', release, recoverable=True)
    task['_executionSession'].hold_resource('admission', lambda _: calls.append('admission'))
    with pytest.raises(asyncio.CancelledError):
        runtime.finish(task['id'], result={'ok': True})
    assert not task['_executionTerminalizing']
    assert runtime.finish(task['id'], result={'ok': True})
    assert task['status'] == 'error'
    assert calls.count('route') == calls.count('admission') == 1


@pytest.mark.parametrize('error_type', [asyncio.CancelledError, SystemExit])
def test_interrupted_unbound_rollback_still_releases_existing_resources(error_type):
    from lib.agent_core.execution_session import bind_model_route
    execution = session('unbound-rollback-' + error_type.__name__)
    calls = []
    execution.hold_resource('model_route', lambda _: calls.append('existing route'))
    execution.hold_resource('admission', lambda _: calls.append('admission'), release_order=-1)
    error = error_type('new route rollback interrupted')
    def rollback():
        calls.append('new route')
        raise error
    with pytest.raises(error_type) as caught:
        bind_model_route(execution, rollback)
    assert caught.value is error
    assert calls == ['new route', 'existing route', 'admission']
    assert execution.settle(ExecutionPhase.COMPLETED).outcome is ExecutionPhase.FAILED
    assert calls == ['new route', 'existing route', 'admission']

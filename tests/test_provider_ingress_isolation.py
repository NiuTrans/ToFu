"""Provider ingress is execution-owned; storage and delivery are observers.

These tests inject deliberately slow database/push seams.  They assert the
seams are absent from the callback stack while an upstream model dispatch is
active, then re-enter normally at the provider boundary for convergence.
"""

from __future__ import annotations

import threading
import time

import pytest

from lib.agent_core.events import EventType
from lib.tasks_pkg.manager._provider_ingress_guard import (
    active_provider_ingress_token,
    begin_provider_ingress,
    end_provider_ingress,
    record_deferred_observer_event,
)
from lib.tasks_pkg.manager.runtime import chat_task_runtime


pytestmark = pytest.mark.unit


def _attempt_task(task_id: str) -> dict:
    chat_task_runtime.discard(task_id)
    task = chat_task_runtime.create(user_id=1, task_id=task_id)
    task.update({
        'convId': f'conv-{task_id}',
        '_turnId': f'turn-{task_id}',
        '_attemptId': f'attempt-{task_id}',
        'status': 'running',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'phase': None,
    })
    return task


def test_slow_storage_and_push_never_enter_provider_callback(
        monkeypatch):
    """A provider delta stays fast even if both observer seams would block."""
    import lib.agent_core.push as push_module
    import lib.tasks_pkg.event_log as event_log
    import lib.tasks_pkg.manager._events as event_manager
    import lib.turn_lifecycle as turn_lifecycle

    task = _attempt_task('provider-observer-isolation')
    storage_calls = []
    push_calls = []
    slow = {'enabled': True}

    def _record(*_args, **_kwargs):
        storage_calls.append(time.perf_counter())
        if slow['enabled']:
            time.sleep(0.25)
        return 'carried'

    def _push(*_args, **_kwargs):
        push_calls.append(time.perf_counter())
        if slow['enabled']:
            time.sleep(0.25)

    monkeypatch.setattr(turn_lifecycle, 'record_task_event', _record)
    monkeypatch.setattr(push_module, 'push_event', _push)
    monkeypatch.setattr(
        event_log, 'project_persistent_event',
        lambda _task_id, event: dict(event),
    )
    monkeypatch.setattr(
        event_log, 'append_persistent_event', lambda *_args, **_kwargs: None)

    token = begin_provider_ingress(task, span_id='model:test:wire:1')
    task['_pushWithheldAt'] = 123.0
    started = time.perf_counter()
    event_manager.append_event(task, {
        'type': EventType.DELTA,
        'content': 'upstream bytes',
    })
    callback_elapsed = time.perf_counter() - started
    # The boundary flush waits for the bounded delivery worker: persistence
    # and push did happen (streamed live), just never on the drain thread.
    receipt = end_provider_ingress(task, token=token)

    try:
        assert callback_elapsed < 0.08
        assert len(storage_calls) == 1
        assert len(push_calls) == 1
        # A successful async persist clears an earlier delivery wedge, the
        # same as the synchronous authoritative path would.
        assert '_pushWithheldAt' not in task
        assert receipt['deferredEvents'] == 1
        assert receipt['deliveredEvents'] == 1
        assert receipt['droppedEvents'] == 0
        assert receipt['eventTypes'] == [EventType.DELTA]

        # Once upstream consumption is over, the ordinary authoritative seam
        # resumes synchronously.
        slow['enabled'] = False
        event_manager.append_event(task, {
            'type': EventType.DELTA,
            'content': 'post-ingress convergence',
        })
        assert len(storage_calls) == 2
        assert len(push_calls) == 2
    finally:
        chat_task_runtime.discard(task['id'])


def test_stream_checkpoint_wait_happens_only_after_provider_return(monkeypatch):
    """A slow recovery checkpoint cannot delay a provider delta callback."""
    import lib.tasks_pkg.manager._stream as stream_module

    task = {
        'id': 'provider-checkpoint-isolation',
        '_attemptId': 'attempt-checkpoint-isolation',
        '_userId': 1,
        'convId': 'conv-checkpoint-isolation',
        'status': 'running',
        'content': '',
        'thinking': '',
        'content_lock': threading.Lock(),
        'events_lock': threading.Lock(),
        'model': 'test-model',
        'config': {},
    }
    observed = []
    checkpoint_calls = []
    callback_elapsed = []

    def _append(_task, event):
        observed.append((
            event.get('type'),
            active_provider_ingress_token(_task) is not None,
            event.get('observerIsolation'),
        ))

    def _slow_checkpoint(_task):
        checkpoint_calls.append(time.perf_counter())
        time.sleep(0.20)

    def _dispatch(_body, *, on_content, **_kwargs):
        started = time.perf_counter()
        on_content('faithful output')
        callback_elapsed.append(time.perf_counter() - started)
        assert checkpoint_calls == []
        return ({'role': 'assistant', 'content': 'faithful output',
                 'tool_calls': []}, 'stop', {})

    monkeypatch.setattr(stream_module, 'append_event', _append)
    monkeypatch.setattr(
        stream_module, 'checkpoint_task_partial', _slow_checkpoint)
    monkeypatch.setattr(stream_module, 'dispatch_stream', _dispatch)

    result = stream_module.stream_llm_response(
        task, {'model': 'test-model', 'messages': []})

    assert result.message['content'] == 'faithful output'
    assert callback_elapsed[0] < 0.08
    assert len(checkpoint_calls) == 1
    assert active_provider_ingress_token(task) is None
    assert '_providerIngressIsolation' not in task
    assert '_providerIngressIsolationLock' not in task
    assert any(item[:2] == (EventType.DELTA, True) for item in observed)
    completed = next(
        item for item in observed
        if item[0] == EventType.MODEL_REQUEST_COMPLETE)
    assert completed[1] is False
    assert completed[2] == {
        'contract': 'tofu.provider-ingress-isolation/v1',
        'providerDispatches': 1,
        'deferredEvents': 0,
        'deferredCheckpoints': 1,
    }


def test_registry_repair_never_reads_storage_on_provider_ingress(monkeypatch):
    """A vanished runtime entry is repaired only after provider return."""
    import lib.tasks_pkg.manager._events as event_manager

    task = _attempt_task('provider-registry-repair-isolation')
    chat_task_runtime.discard(task['id'])
    repair_calls = []

    def _slow_repair(_task):
        repair_calls.append(True)
        time.sleep(0.25)
        return False

    monkeypatch.setattr(event_manager, '_try_readopt_task', _slow_repair)
    token = begin_provider_ingress(task, span_id='model:repair:wire:1')
    started = time.perf_counter()
    event_manager.append_event(task, {
        'type': EventType.DELTA,
        'content': 'still accumulated by the stream owner',
    })
    elapsed = time.perf_counter() - started
    end_provider_ingress(task, token=token)

    assert elapsed < 0.08
    assert repair_calls == []
    assert task['_registryWithheldCount'] == 1


def test_ingress_delivery_is_fifo_and_flushed_at_boundary():
    """Queued deliveries run in sequence order and finish before the
    provider boundary returns."""
    from lib.tasks_pkg.manager._provider_ingress_guard import (
        enqueue_ingress_delivery,
    )

    task = {'id': 'provider-delivery-fifo'}
    token = begin_provider_ingress(task, span_id='model:fifo:wire:1')
    delivered = []
    for sequence in range(5):
        assert enqueue_ingress_delivery(
            task,
            token=token,
            sequence=sequence,
            event_type=EventType.DELTA,
            deliver=lambda s=sequence: delivered.append(s),
        )
    receipt = end_provider_ingress(task, token=token)

    assert delivered == [0, 1, 2, 3, 4]
    assert receipt['deliveredEvents'] == 5
    assert receipt['droppedEvents'] == 0
    assert receipt['deliveryFailures'] == 0


def test_ingress_delivery_drops_oldest_when_observers_lag(monkeypatch):
    """A full queue must never block the drain thread: the oldest
    undelivered event is dropped and accounted."""
    import lib.tasks_pkg.manager._provider_ingress_guard as guard

    monkeypatch.setattr(guard, '_DELIVERY_QUEUE_MAX', 4)
    task = {'id': 'provider-delivery-drop'}
    token = guard.begin_provider_ingress(task, span_id='model:drop:wire:1')
    delivered = []
    worker_busy = threading.Event()
    release_worker = threading.Event()

    def _blocking_deliver():
        worker_busy.set()
        release_worker.wait(timeout=5)
        delivered.append(0)

    assert guard.enqueue_ingress_delivery(
        task, token=token, sequence=0, event_type=EventType.DELTA,
        deliver=_blocking_deliver)
    assert worker_busy.wait(timeout=5)
    # Six more into a capacity-4 queue: two oldest get dropped.
    for sequence in range(1, 7):
        assert guard.enqueue_ingress_delivery(
            task, token=token, sequence=sequence, event_type=EventType.DELTA,
            deliver=lambda s=sequence: delivered.append(s))
    release_worker.set()
    receipt = guard.end_provider_ingress(task, token=token)

    assert receipt['droppedEvents'] == 2
    assert delivered == [0, 3, 4, 5, 6]
    assert receipt['deliveredEvents'] == 5


def test_ingress_delivery_stale_attempt_fence_drops_rest_quietly():
    """Once persistence fences the attempt as stale, remaining queued
    closures are dropped instead of flooding identical failures."""
    import lib.tasks_pkg.manager._provider_ingress_guard as guard

    task = {'id': 'provider-delivery-fence'}
    token = guard.begin_provider_ingress(task, span_id='model:fence:wire:1')
    attempted = []
    first_ran = threading.Event()

    def _fenced_deliver():
        attempted.append(0)
        task['aborted'] = True
        first_ran.set()
        raise RuntimeError(
            'conversation event rejected: attempt is stale or no longer current')

    assert guard.enqueue_ingress_delivery(
        task, token=token, sequence=0, event_type=EventType.DELTA,
        deliver=_fenced_deliver)
    assert first_ran.wait(timeout=5)
    for sequence in (1, 2):
        assert guard.enqueue_ingress_delivery(
            task, token=token, sequence=sequence, event_type=EventType.DELTA,
            deliver=lambda s=sequence: attempted.append(s))
    receipt = guard.end_provider_ingress(task, token=token)

    assert attempted == [0]
    assert receipt['deliveryFailures'] == 1
    assert receipt['droppedEvents'] == 2
    assert receipt['deliveredEvents'] == 0

def test_provider_ingress_receipt_has_fixed_shape_and_bounded_types():
    task = {'id': 'provider-receipt-budget'}
    token = begin_provider_ingress(task, span_id='model:budget:wire:1')
    for sequence in range(100):
        record_deferred_observer_event(
            task,
            token=token,
            sequence=sequence,
            event_type=f'event-{sequence}',
        )
    receipt = end_provider_ingress(task, token=token)

    assert receipt['deferredEvents'] == 100
    assert receipt['firstDeferredSeq'] == 0
    assert receipt['lastDeferredSeq'] == 99
    assert len(receipt['eventTypes']) == 16
    assert receipt['active'] is False

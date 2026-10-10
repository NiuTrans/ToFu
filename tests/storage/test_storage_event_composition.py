"""Native-advertised composition leaves admission, cancellation and ACK intact."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.event_batcher import StorageEventBatcher, _PendingEvent
from lib.storage.event_composition import native_event_batch_composition_budget
from lib.storage.v2_client import StorageV2Client

pytestmark = pytest.mark.unit


def declaration(cap=4096, headroom=1024):
    return {'contract': 'tofu.native-event-batch-composition/v1',
        'maximumCarriedPayloadBytes': cap, 'transactionHeadroomBytes': headroom,
        'streamSegmentHeaderBytes': 53, 'streamEventHeaderBytes': 13,
        'eventEnvelopeBytes': 6, 'metadataBaseBytes': 900,
        'metadataTaskIdCopies': 10, 'metadataTypeCopies': 4}


class Client:
    def __init__(self, budget=None, *, block_first=False, fail_first=None):
        self.budget = budget
        self.calls = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.fail_first = fail_first
        if not block_first:
            self.release.set()

    def event_batch_composition_budget(self):
        return self.budget

    def command(self, operation, payload, *_args, **kwargs):
        assert operation == 'event.append_batch'
        assert kwargs['deadline'] == 2.0
        self.calls.append(payload['events'][:])
        self.started.set()
        if len(self.calls) == 1:
            assert self.release.wait(5)
            if self.fail_first is not None:
                raise self.fail_first
        rows = payload['events']
        return {'results': [{'inserted': True, 'task_id': row['task_id'],
            'sequence': row['sequence']} for row in rows], 'inserted': len(rows), 'deduplicated': 0}


def paused_provider(client):
    started, release = threading.Event(), threading.Event()
    def provider(**_kwargs):
        started.set()
        assert release.wait(5)
        return client
    return provider, started, release


@pytest.mark.parametrize('field,value', [('maximumCarriedPayloadBytes', True),
    ('transactionHeadroomBytes', 4096), ('eventEnvelopeBytes', -1),
    ('metadataBaseBytes', '900'), ('streamEventHeaderBytes', 0),
    ('metadataTaskIdCopies', None), ('contract', 'future/v2')])
def test_invalid_capability_retains_legacy_grouping(field, value):
    advertised = declaration(); advertised[field] = value
    assert native_event_batch_composition_budget(advertised) is None
    assert native_event_batch_composition_budget(None) is None


def test_native_accessor_uses_cached_cap_without_query_or_health():
    client = StorageV2Client.__new__(StorageV2Client)
    client._readiness_lock = threading.Lock()
    client._pool = type('Closed', (), {'close': lambda self: None, 'resize': lambda self, _capacity: None})()
    client.set_readiness_budget({'eventBatchComposition': declaration()})
    client._call = lambda *_args, **_kwargs: pytest.fail('capability must not query')
    client._health_call = lambda *_args, **_kwargs: pytest.fail('capability must not probe')
    assert client.event_batch_composition_budget().maximum_carried_payload_bytes == 4096
    client.set_readiness_budget({})
    assert client.event_batch_composition_budget() is None
    # Suppress production finalizer on this intentionally pool-less adapter.


def test_native_groups_keep_mixed_task_fences_order_and_barrier_after_prior_ack():
    budget = native_event_batch_composition_budget(declaration())
    client = Client(budget, block_first=True)
    provider, ready, release = paused_provider(client)
    committed = []
    batcher = StorageEventBatcher(provider, coalesce_ms=0,
        on_commit=lambda task_ids: committed.append(task_ids))
    try:
        for index in range(5):
            batcher.append(f't{index}', index, {'type': 'delta', 'content': 'x' * 500},
                require_attempts_live=[f'a{index}'], wait=False)
        assert ready.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            flushed = pool.submit(batcher.flush, 5)
            release.set()
            assert client.started.wait(5)
            assert not flushed.done()
            client.release.set()
            assert flushed.result(5)
        flat = [row for group in client.calls for row in group]
        assert [row['task_id'] for row in flat] == [f't{index}' for index in range(5)]
        assert [row['require_attempts_live'] for row in flat] == [[f'a{index}'] for index in range(5)]
        assert [len(group) for group in client.calls] == [1] * 5
        assert batcher.metrics['inserted'] == 5
        assert len(committed) == 5
        assert batcher.metrics['event_byte_capacity'] > budget.maximum_carried_payload_bytes
    finally:
        release.set(); client.release.set()
        assert batcher.close(5)


@pytest.mark.parametrize('native', [False, True])
def test_sql_or_old_native_without_capability_keeps_original_wire_batch(native):
    client = Client(None)
    if not native:
        client.event_batch_composition_budget = None
    provider, ready, release = paused_provider(client)
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        for index in range(5):
            batcher.append(f'old-{index}', 1, {'type': 'delta', 'content': 'x' * 8192}, wait=False)
        assert ready.wait(5)
        release.set(); assert batcher.flush(5)
        assert [len(group) for group in client.calls] == [5]
        assert batcher.metrics['composition_byte_capacity'] == 0
    finally:
        release.set(); assert batcher.close(5)


def test_large_singleton_keeps_original_admission_and_following_event():
    client = Client(native_event_batch_composition_budget(declaration(2 * 1024 * 1024, 512 * 1024)))
    provider, ready, release = paused_provider(client)
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        assert batcher.append('large', 1, {'type': 'delta', 'content': 'x' * (2 * 1024 * 1024)}, wait=False)['accepted']
        assert ready.wait(5)
        batcher.append('small', 1, {'type': 'delta', 'content': 'x' * 8192}, wait=False)
        original_admission = batcher.metrics['event_byte_capacity']
        release.set(); assert batcher.flush(5)
        assert [len(group) for group in client.calls] == [1, 1]
        assert client.calls[0][0]['task_id'] == 'large'
        assert batcher.metrics['event_byte_capacity'] == original_admission
        assert batcher.metrics['inserted'] == 2
    finally:
        release.set(); assert batcher.close(5)


def test_failed_group_returns_original_error_once_and_next_group_still_confirms():
    failure = StorageError('database_internal', 'synthetic source failure')
    client = Client(native_event_batch_composition_budget(declaration()), fail_first=failure)
    provider, ready, release = paused_provider(client)
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(batcher.append, 'first', 1, {'type': 'delta', 'content': 'x' * 500})
            assert ready.wait(5)
            second = pool.submit(batcher.append, 'second', 1, {'type': 'delta', 'content': 'y' * 500})
            release.set()
            with pytest.raises(StorageError) as raised:
                first.result(5)
            assert raised.value is failure
            assert second.result(5)['inserted'] is True
        assert len(client.calls) == 2
        assert batcher.metrics['failed'] == 1 and batcher.metrics['inserted'] == 1
    finally:
        release.set(); assert batcher.close(5)


def test_blocked_provider_leaves_queue_cancellable_and_close_does_not_hold_queue_lock():
    client = Client(native_event_batch_composition_budget(declaration()))
    provider, ready, release = paused_provider(client)
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(batcher.append, 'cancelled', 1, {'type':'delta'}, timeout=.05)
            assert ready.wait(5)
            # Both enqueue and timeout cancellation must acquire the queue
            # condition while selected-provider initialization is blocked.
            assert batcher.append('later', 1, {'type':'delta'}, wait=False)['accepted']
            with pytest.raises(StorageError) as raised:
                first.result(5)
            assert raised.value.code == 'database_timeout'
            assert batcher.metrics['cancelled_before_start'] == 1
            assert batcher.close(.1) is False
            assert batcher.metrics['queue_depth'] == 0
            release.set()
        assert batcher.close(5)
        assert client.calls == []
    finally:
        release.set(); batcher.close(5)


def test_later_group_remains_cancellable_while_first_rpc_is_in_flight():
    client = Client(native_event_batch_composition_budget(declaration()), block_first=True)
    batcher = StorageEventBatcher(lambda **_kwargs: client, coalesce_ms=0)
    try:
        batcher.append('inflight', 1, {'type':'delta', 'content':'x'*500}, wait=False)
        assert client.started.wait(5)
        with pytest.raises(StorageError) as raised:
            batcher.append('cancel-before-dispatch', 1, {'type':'delta', 'content':'y'*500}, timeout=.05)
        assert raised.value.code == 'database_timeout'
        assert batcher.metrics['cancelled_before_start'] == 1
        client.release.set()
        with pytest.raises(StorageError) as barrier_failure:
            batcher.flush(5)
        assert barrier_failure.value.code == 'database_timeout'
        assert len(client.calls) == 1
        assert client.calls[0][0]['task_id'] == 'inflight'
    finally:
        client.release.set(); assert batcher.close(5)


def test_cancelled_head_does_not_give_next_caller_old_provider_error():
    client = Client(native_event_batch_composition_budget(declaration()))
    started, release = threading.Event(), threading.Event()
    calls = 0
    def provider(**_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set(); assert release.wait(5)
            raise StorageError('database_unavailable', 'cancelled provider startup')
        return client
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(batcher.append, 'cancelled-head', 1, {'type':'delta'}, timeout=.05)
            assert started.wait(5)
            second = pool.submit(batcher.append, 'replacement', 1, {'type':'delta'})
            with pytest.raises(StorageError) as raised:
                first.result(5)
            assert raised.value.code == 'database_timeout'
            release.set()
            assert second.result(5)['inserted'] is True
        assert len(client.calls) == 1 and client.calls[0][0]['task_id'] == 'replacement'
    finally:
        release.set(); assert batcher.close(5)


def test_empty_flush_does_not_initialize_selected_client_or_fail_on_provider():
    def provider(**_kwargs):
        pytest.fail('empty durability barrier must not initialize a backend')
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        assert batcher.flush(5)
    finally:
        assert batcher.close(5)


def test_exact_composition_boundary_includes_two_rows_but_not_third():
    prototype = native_event_batch_composition_budget(declaration())
    client = Client(prototype)
    provider, ready, release = paused_provider(client)
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        pending = batcher._build_pending_event('t0', 1, {'type':'delta','content':'x'*500})
        fee = prototype.estimate(pending.raw_event_json_bytes, pending.task_id_bytes,
            pending.event_type_bytes, pending.logical_type_bytes)
        client.budget = native_event_batch_composition_budget(declaration(fee * 2 + 1024, 1024))
        for index in range(3):
            batcher.append(f't{index}', 1, {'type':'delta','content':'x'*500}, wait=False)
        assert ready.wait(5); release.set(); assert batcher.flush(5)
        assert [len(group) for group in client.calls] == [2, 1]
        assert batcher.metrics['max_estimated_composition_bytes'] == fee * 2
    finally:
        release.set(); assert batcher.close(5)


@pytest.mark.parametrize('boundary', ['getter', 'attribute'])
def test_optional_capability_exception_keeps_valid_command_ack(boundary):
    class BrokenCapability(Client):
        def __getattribute__(self, name):
            if name == 'event_batch_composition_budget' and boundary == 'attribute':
                raise ValueError('invalid cached capability access')
            return super().__getattribute__(name)
        def event_batch_composition_budget(self):
            raise ValueError('invalid cached capability decode')
    client = BrokenCapability()
    batcher = StorageEventBatcher(lambda **_kwargs: client, coalesce_ms=0)
    try:
        assert batcher.append('valid-command', 1, {'type':'delta'})['inserted'] is True
        assert len(client.calls) == 1
        assert batcher.metrics['composition_byte_capacity'] == 0
        assert batcher.metrics['failed'] == 0
    finally:
        assert batcher.close(5)


@pytest.mark.parametrize('first_failure', [False, True])
def test_future_cancel_does_not_pollute_earlier_barrier_and_earlier_rpc_failure_wins(first_failure):
    error = StorageError('database_internal', 'earlier real RPC failed') if first_failure else None
    client = Client(native_event_batch_composition_budget(declaration()), block_first=True, fail_first=error)
    batcher = StorageEventBatcher(lambda **_kwargs: client, coalesce_ms=0)
    try:
        batcher.append('prefix', 1, {'type':'delta','content':'x'*500}, wait=False)
        assert client.started.wait(5)
        barrier = _PendingEvent(None); batcher._enqueue(barrier)
        assert barrier.barrier_prefix_ordinal == 1
        with pytest.raises(StorageError):
            batcher.append('future-cancelled', 1, {'type':'delta','content':'y'*500}, timeout=.05)
        assert batcher._prefix_failure.ordinal == 2
        client.release.set(); assert barrier.done.wait(5)
        if first_failure:
            assert barrier.error.code == 'database_internal'
            assert barrier.error is not error
            assert batcher._prefix_failure.ordinal == 1
        else:
            assert barrier.error is None
        # New barriers cover the cancelled accepted event. Later success or
        # a failed earlier flush is not proof that this event is durable.
        for _ in range(2):
            with pytest.raises(StorageError) as raised:
                batcher.flush(5)
            assert raised.value.code == ('database_internal' if first_failure else 'database_timeout')
        assert len(client.calls) == 1
        from dataclasses import fields
        assert all(type(getattr(batcher._prefix_failure, field.name)) in (str, int, bool, type(None))
                   for field in fields(batcher._prefix_failure))
    finally:
        client.release.set(); assert batcher.close(5)
        assert batcher._prefix_failure is None


def test_future_provider_failure_cannot_invalidate_head_barrier():
    client = Client(native_event_batch_composition_budget(declaration()), block_first=True)
    calls = 0
    failure = StorageError('database_unavailable', 'future provider failed')
    def provider(**_kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise failure
        return client
    batcher = StorageEventBatcher(provider, coalesce_ms=0)
    try:
        batcher.append('previous', 1, {'type':'delta','content':'x'*500}, wait=False)
        assert client.started.wait(5)
        barrier = _PendingEvent(None); batcher._enqueue(barrier)
        future = batcher._build_pending_event('future', 1, {'type':'delta'}); batcher._enqueue(future)
        client.release.set()
        assert barrier.done.wait(5) and barrier.error is None
        assert future.done.wait(5) and future.error is failure
        assert calls == 2 and len(client.calls) == 1
    finally:
        client.release.set(); assert batcher.close(5)


def test_observational_callback_failure_and_claimed_timeout_do_not_invent_failed_prefix():
    client = Client(native_event_batch_composition_budget(declaration()), block_first=True)
    def callback(_ids):
        raise ValueError('observational callback')
    batcher = StorageEventBatcher(lambda **_kwargs: client, coalesce_ms=0, on_commit=callback)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(batcher.append, 'claimed', 1, {'type':'delta'}, timeout=.05)
            assert client.started.wait(5)
            with pytest.raises(StorageError) as raised:
                first.result(5)
            assert raised.value.code == 'database_timeout'
        assert batcher.metrics['cancelled_before_start'] == 0
        assert batcher._prefix_failure is None
        client.release.set(); assert batcher.flush(5)
        assert batcher._prefix_failure is None and batcher.metrics['inserted'] == 1
    finally:
        client.release.set(); assert batcher.close(5)


def test_mixed_dedup_success_and_attempt_rejection_keep_results_but_reject_durability_barrier():
    class Mixed(Client):
        def command(self, operation, payload, *_args, **_kwargs):
            assert operation == 'event.append_batch'
            self.calls.append(payload['events'][:])
            results = []
            for row in payload['events']:
                result = {'task_id':row['task_id'], 'sequence':row['sequence'],
                          'inserted':row['task_id'] == 'success'}
                if row['task_id'] == 'rejected':
                    result['rejected'] = 'attempt_not_live'
                results.append(result)
            return {'results':results, 'inserted':1, 'deduplicated':1, 'rejected':1}
    client = Mixed(native_event_batch_composition_budget(declaration(65536, 16384)))
    provider, ready, release = paused_provider(client)
    committed = []
    batcher = StorageEventBatcher(provider, coalesce_ms=0,
        on_commit=lambda ids: committed.append(ids))
    try:
        pending = []
        for task in ['dedup', 'success', 'rejected']:
            item = batcher._build_pending_event(task, 1, {'type':'delta'}, ['attempt'])
            batcher._enqueue(item); pending.append(item)
        assert ready.wait(5); release.set()
        for item in pending:
            assert item.done.wait(5) and item.error is None
        assert pending[0].result['inserted'] is False and 'rejected' not in pending[0].result
        assert pending[1].result['inserted'] is True
        assert pending[2].result['rejected'] == 'attempt_not_live'
        assert batcher._prefix_failure.ordinal == pending[2].accepted_ordinal
        assert committed == [frozenset(['dedup','success','rejected'])]
        assert len(client.calls) == 1
        with pytest.raises(StorageError) as raised:
            batcher.flush(5)
        assert raised.value.code == 'database_conflict'
    finally:
        release.set(); assert batcher.close(5)

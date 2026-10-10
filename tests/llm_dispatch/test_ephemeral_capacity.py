"""Route fan-out, concurrent admission and failed construction release resources."""
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import asyncio
import copy
import threading

import pytest

from lib.llm_dispatch import ephemeral
from lib.model_routing import dispatch_adapter as adapter
from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
from tests.model_routing.test_model_routing_contract import _full_document

pytestmark = pytest.mark.unit


@pytest.fixture
def pool(monkeypatch):
    from lib.llm_dispatch import factory
    dispatcher = SimpleNamespace(slots=[], _lock=threading.Lock(), initialize=lambda: None)
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(ephemeral, '_handles', {})
    monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '0')
    monkeypatch.delenv('TOFU_EPHEMERAL_SLOT_CAPACITY', raising=False)
    for name, value in {
        'TOFU_AGENT_WORKERS': 32, 'TOFU_MAX_INFLIGHT_TASKS': 32,
        'TOFU_SYNC_WORKERS': 16, 'TOFU_SWARM_GLOBAL_WORKERS': 16,
        'TOFU_SWARM_MAX_PARALLEL': 4, 'TOFU_SWARM_SESSION_CAPACITY': 8,
    }.items():
        monkeypatch.setenv(name, str(value))
    yield dispatcher
    for handle in list(ephemeral._handles.values()):
        ephemeral.dispose_ephemeral_slot(handle)


def _repository():
    document = _full_document()
    for connection in document['connections']:
        connection['base_url'] = 'http://127.0.0.1:1/v1'
    prototype = document['deployments'][0]
    document['deployments'] = [
        {**copy.deepcopy(prototype), 'deployment_id': f'deployment-{index}',
         'wire_model_id': f'wire-{index}'} for index in range(64)
    ]
    return SimpleNamespace(get=lambda owner: SimpleNamespace(document=document))


def _group(repository, owner=23):
    return adapter.mint_routed_slot_group(
        repository, OwnerBoundary.create(owner),
        NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a'),
        owner_tag=f'task:{owner}')


def _mint():
    return ephemeral.mint_ephemeral_slot(
        base_url='http://127.0.0.1:1/v1', api_key='', model_id='test-model')


def test_concurrent_tasks_can_use_full_route_groups_beyond_sixteen(pool):
    repository = _repository()
    groups = []
    try:
        for owner in range(20, 40):
            groups.append(_group(repository, owner))
        assert all(len(group.handles) == 64 for group in groups)
        assert ephemeral.count_ephemeral_slots() == 1280
        assert len({group.pin_id for group in groups}) == 20
        for group in groups:
            assert all(handle.slot.provider_id == group.pin_id for handle in group.handles)
    finally:
        for group in groups:
            adapter.dispose_routed_slot_group(group)
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []


def test_concurrent_registration_keeps_every_handle_owned(pool, monkeypatch):
    from lib.llm_dispatch import factory
    barrier = threading.Barrier(2)
    def dispatcher():
        barrier.wait(timeout=5)
        return pool
    monkeypatch.setattr(factory, 'get_dispatcher', dispatcher)
    monkeypatch.setenv('TOFU_EPHEMERAL_SLOT_CAPACITY', '1')
    def attempt():
        try:
            return _mint()
        except RuntimeError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        handles = list(executor.map(lambda _: attempt(), range(2)))
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: pool)
    assert sum(handle is not None for handle in handles) == 2
    assert ephemeral.count_ephemeral_slots() == len(pool.slots) == 2


def test_observer_failure_cannot_orphan_handles_or_stop_group_cleanup(pool, monkeypatch):
    def broken_audit(*args, **kwargs):
        raise RuntimeError('audit sink unavailable')
    monkeypatch.setattr(ephemeral, 'audit_log', broken_audit)
    group = _group(_repository())
    assert ephemeral.count_ephemeral_slots() == 64
    assert adapter.dispose_routed_slot_group(group)
    assert not adapter.dispose_routed_slot_group(group)
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []


@pytest.mark.parametrize('failure', [RuntimeError('mint failed'), asyncio.CancelledError()])
def test_partial_group_rolls_back_on_error_or_cancellation(pool, monkeypatch, failure):
    original_mint = adapter.mint_ephemeral_slot
    def mint(**kwargs):
        if ephemeral.count_ephemeral_slots() == 3:
            raise failure
        return original_mint(**kwargs)
    monkeypatch.setattr(adapter, 'mint_ephemeral_slot', mint)
    with pytest.raises(type(failure)):
        _group(_repository())
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []


def test_group_disposal_releases_siblings_and_remains_retryable(pool, monkeypatch):
    group = _group(_repository())
    original_dispose = adapter.dispose_ephemeral_slot
    failed_handle = group.handles[0]
    def dispose(handle):
        if handle is failed_handle:
            raise RuntimeError('temporary disposal failure')
        return original_dispose(handle)
    monkeypatch.setattr(adapter, 'dispose_ephemeral_slot', dispose)
    with pytest.raises(RuntimeError, match='temporary disposal failure'):
        adapter.dispose_routed_slot_group(group)
    assert ephemeral.count_ephemeral_slots() == 1
    assert pool.slots == [failed_handle.slot]
    monkeypatch.setattr(adapter, 'dispose_ephemeral_slot', original_dispose)
    assert adapter.dispose_routed_slot_group(group)
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []


def test_old_capacity_cannot_refuse_new_group_or_dispose_an_existing_owner(pool, monkeypatch):
    monkeypatch.setenv('TOFU_EPHEMERAL_SLOT_CAPACITY', '1')
    first, second = _group(_repository()), _group(_repository(), 24)
    assert ephemeral.count_ephemeral_slots() == 128
    adapter.dispose_routed_slot_group(first)
    assert pool.slots == [handle.slot for handle in second.handles]
    adapter.dispose_routed_slot_group(second)
    assert not pool.slots


def test_cancellation_before_handle_handoff_rolls_back_registration(pool, monkeypatch):
    original_audit = ephemeral.audit_log
    def cancel_mint(event, **details):
        if event == 'ephemeral_slot_mint':
            raise asyncio.CancelledError()
        return original_audit(event, **details)
    monkeypatch.setattr(ephemeral, 'audit_log', cancel_mint)
    with pytest.raises(asyncio.CancelledError):
        _mint()
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []


def test_high_rpm_history_cannot_make_a_healthy_target_overflow_or_unselectable():
    import math
    import time
    from lib.llm_dispatch.slot import Slot
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    slot = Slot(key_name='test', api_key='test', model='test', capabilities={'text'}, rpm_limit=1)
    slot.rpm_window = [time.time()] * 10_000
    dispatcher = LLMDispatcher()
    dispatcher._initialized = True
    dispatcher.slots = [slot]
    dispatcher.resize_resource_capacity(1)
    assert math.isfinite(slot.score())
    assert all(dispatcher.pick_top_n(1) == [slot] for _ in range(64))
    assert slot.inflight == 64
    for _ in range(64):
        slot.release()
    assert slot.inflight == 0

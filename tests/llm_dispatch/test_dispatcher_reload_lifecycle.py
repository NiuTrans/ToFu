"""Configuration reload preserves live request routes and their settlement."""

from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

import pytest

from lib.llm_dispatch import ephemeral, factory, health_local
from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.provider_pin import provider_pin
from lib.llm_dispatch.slot import Slot


pytestmark = pytest.mark.unit


@pytest.fixture
def pool(monkeypatch):
    state = SimpleNamespace(revision=1, fail=False, handles=[])
    monkeypatch.setattr(factory, '_default_factory', factory.DispatcherFactory())
    monkeypatch.setattr(ephemeral, '_handles', {})
    monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '0')
    monkeypatch.setattr('lib.proxy.register_no_proxy_url', lambda url: None)

    def build(dispatcher):
        dispatcher.slots.append(Slot(
            key_name=f'configured-{state.revision}', api_key='synthetic',
            model='reload-model', capabilities={'text'},
            provider_id='configured-provider'))
        if state.fail:
            raise ValueError('injected configuration failure')

    monkeypatch.setattr(LLMDispatcher, '_build_slots', build)
    monkeypatch.setattr(LLMDispatcher, '_load_benchmark_data', lambda self: None)
    monkeypatch.setattr(health_local, '_get_dispatcher', factory.get_dispatcher)
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)

    def mint(pin, rank=0):
        handle = ephemeral.mint_ephemeral_slot(
            base_url='http://10.0.0.5:8080/v1', api_key='synthetic',
            model_id='reload-model', capabilities={'text'},
            owner=pin, provider_pin_id=pin)
        handle.slot.route_rank = rank
        state.handles.append(handle)
        return handle

    state.mint = mint
    yield state
    for handle in state.handles:
        ephemeral.dispose_ephemeral_slot(handle)


@pytest.mark.parametrize('reload_kind', ['settings', 'health'])
def test_reload_preserves_live_route_failover_and_disposal(pool, reload_kind):
    primary = pool.mint('request-a', 0)
    backup = pool.mint('request-a', 1)
    foreign = pool.mint('request-b', 0)
    dispatcher = factory.get_dispatcher()
    primary.slot.record_request()
    primary.slot.route_snapshot = {'actual_provider_id': 'provider-a'}
    pool.revision = 2

    if reload_kind == 'settings':
        factory.reset_dispatcher()
    else:
        health_local._rebuild_dispatcher_slots()
    current = factory.get_dispatcher()
    current.initialize()

    assert any(slot is primary.slot for slot in current.slots)
    assert primary.slot.inflight == 1
    assert primary.slot.route_snapshot == {'actual_provider_id': 'provider-a'}
    assert ephemeral.count_ephemeral_slots() == 3
    assert [s.key_name for s in current.slots if not s.key_name.startswith(
        'ephemeral_')] == ['configured-2']
    with provider_pin('request-a'):
        assert current.pick_slot('text', prefer_model='reload-model',
                            strict_model=True,
                            exclude_keys={primary.slot.key_name}) is backup.slot
        assert current.pick_slot('text', prefer_model='reload-model',
                            strict_model=True, exclude_keys={
                                primary.slot.key_name, backup.slot.key_name,
                            }) is None
    assert foreign.slot.inflight == 0

    # A response started before reload settles the same reservation afterwards.
    primary.slot.record_success(12)
    assert primary.slot.inflight == 0
    assert ephemeral.dispose_ephemeral_slot(primary)
    assert all(s is not primary.slot for s in dispatcher.slots)
    assert all(s is not primary.slot for s in current.slots)
    assert not ephemeral.dispose_ephemeral_slot(primary)


def test_failed_rebuild_does_not_drop_routes_or_publish_partial_pool(pool):
    handle = pool.mint('request-a')
    pool.fail = True
    factory.reset_dispatcher()
    current = factory.get_dispatcher()
    with pytest.raises(ValueError, match='injected configuration failure'):
        current.initialize()
    assert current.slots == [handle.slot]

    pool.fail = False
    pool.revision = 3
    current.initialize()
    with provider_pin('request-a'):
        assert current.pick_slot('text', prefer_model='reload-model',
                            strict_model=True) is handle.slot
    assert sum(s is handle.slot for s in current.slots) == 1


def test_repeated_reset_retains_one_slot_per_live_handle(pool):
    handle = pool.mint('request-a')
    for _ in range(8):
        factory.reset_dispatcher()
        current = factory.get_dispatcher()
        current.initialize()
        assert sum(s is handle.slot for s in current.slots) == 1
        assert len(current.slots) == 2
        assert ephemeral.count_ephemeral_slots() == 1
    assert ephemeral.dispose_ephemeral_slot(handle)
    factory.reset_dispatcher()
    current = factory.get_dispatcher()
    current.initialize()
    assert len(current.slots) == 1
    assert ephemeral.count_ephemeral_slots() == 0


def test_reload_serializes_with_mint_and_disposal(pool, monkeypatch):
    previous = pool.mint('request-a')
    build_started = threading.Event()
    finish_build = threading.Event()
    build = LLMDispatcher._build_slots

    def blocked_build(dispatcher):
        build_started.set()
        assert finish_build.wait(5), 'test did not release rebuild'
        build(dispatcher)

    monkeypatch.setattr(LLMDispatcher, '_build_slots', blocked_build)
    factory.reset_dispatcher()
    current = factory.get_dispatcher()
    with ThreadPoolExecutor(max_workers=3) as workers:
        reloaded = workers.submit(current.initialize)
        try:
            assert build_started.wait(5)
            minted = workers.submit(pool.mint, 'request-b')
            disposed = workers.submit(ephemeral.dispose_ephemeral_slot, previous)
        finally:
            finish_build.set()
        reloaded.result(timeout=5)
        new = minted.result(timeout=5)
        assert disposed.result(timeout=5)

    assert sum(s is new.slot for s in current.slots) == 1
    assert all(s is not previous.slot for s in current.slots)
    assert ephemeral.count_ephemeral_slots() == 1
    with provider_pin('request-b'):
        assert current.pick_slot('text', prefer_model='reload-model',
                                 strict_model=True) is new.slot


def test_benchmark_refresh_cannot_reseed_live_request_slots(pool, monkeypatch):
    handle = pool.mint('request-a')
    handle.slot.latency_ema = 123

    def seed(dispatcher):
        for slot in dispatcher.slots:
            slot.latency_ema = 999

    monkeypatch.setattr(LLMDispatcher, '_load_benchmark_data', seed)
    factory.reset_dispatcher()
    current = factory.get_dispatcher()
    current.initialize()
    assert handle.slot.latency_ema == 123
    assert [s.latency_ema for s in current.slots if s is not handle.slot] == [999]

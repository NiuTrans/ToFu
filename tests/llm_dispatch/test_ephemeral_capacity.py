"""Route fan-out, concurrent admission and failed construction release resources."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
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
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    dispatcher = LLMDispatcher()
    dispatcher._initialized = True  # request routes never need the configured pool
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr(ephemeral, '_handles', {})
    monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '0')
    monkeypatch.delenv('TOFU_EPHEMERAL_SLOT_CAPACITY', raising=False)
    for name, value in {
        'TOFU_AGENT_WORKERS': 32, 'TOFU_MAX_INFLIGHT_TASKS': 32,
        'TOFU_SYNC_WORKERS': 16,
        'TOFU_SWARM_MAX_PARALLEL': 4, 'TOFU_SWARM_SESSION_CAPACITY': 8,
    }.items():
        monkeypatch.setenv(name, str(value))
    yield dispatcher
    for handle in list(ephemeral._handles.values()):
        ephemeral.dispose_ephemeral_slot(handle)


def _repository(count=64):
    document = _full_document()
    for connection in document['connections']:
        connection['base_url'] = 'http://127.0.0.1:1/v1'
    prototype = document['deployments'][0]
    document['deployments'] = [
        {**copy.deepcopy(prototype), 'deployment_id': f'deployment-{index}',
         'wire_model_id': f'wire-{index}'} for index in range(count)
    ]
    return SimpleNamespace(get=lambda owner: SimpleNamespace(document=document, revision=7))


def _group(repository, owner=23):
    return adapter.mint_routed_slot_group(
        repository, OwnerBoundary.create(owner),
        NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a'),
        owner_tag=f'task:{owner}')


def _mint():
    return ephemeral.mint_ephemeral_slot(
        base_url='http://127.0.0.1:1/v1', api_key='', model_id='test-model')


class _CountingLock:
    """The dispatcher lock, counting how often it is acquired."""

    def __init__(self):
        self._lock = threading.Lock()
        self.acquisitions = 0

    def acquire(self, blocking=True, timeout=-1):
        acquired = self._lock.acquire(blocking, timeout)
        self.acquisitions += int(acquired)
        return acquired

    def release(self):
        self._lock.release()

    def locked(self):
        return self._lock.locked()

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc_info):
        self.release()


def test_group_mint_and_disposal_each_take_one_dispatcher_lock_acquisition(pool):
    from lib.llm_dispatch.slot import Slot
    neighbour = _group(_repository(8), owner=41)
    configured = Slot(key_name='configured', api_key='sk', model='wire-0',
                      capabilities={'text'}, provider_id='provider-a')
    pool.slots.append(configured)
    before = list(pool.slots)
    counting = _CountingLock()
    pool._lock = counting

    group = _group(_repository(52))
    assert counting.acquisitions == 1
    own = [slot for slot in pool.slots if slot.provider_id == group.pin_id]
    assert [id(slot) for slot in own] == [id(handle.slot) for handle in group.handles]
    assert [slot.route_rank for slot in own] == list(range(52))
    others = [slot for slot in pool.slots if slot.provider_id != group.pin_id]
    assert [id(slot) for slot in others] == [id(slot) for slot in before]

    counting.acquisitions = 0
    assert adapter.dispose_routed_slot_group(group)
    assert counting.acquisitions == 1
    assert [id(slot) for slot in pool.slots] == [id(slot) for slot in before]
    assert ephemeral.count_ephemeral_slots() == len(neighbour.handles)
    assert all(not handle.slot.api_key for handle in group.handles)
    adapter.dispose_routed_slot_group(neighbour)


def _spread_over_endpoints(monkeypatch, urls, per_url):
    """Compile ``per_url`` distinct Deployments behind each of ``urls``."""
    from lib.model_routing.routing import compile_candidates
    repository = _repository(1)
    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a')
    prototype = compile_candidates(
        repository.get(OwnerBoundary.create(23)).document, selection)[0]
    candidates = [
        replace(prototype,
                connection={**prototype.connection, 'base_url': url},
                deployment={**prototype.deployment,
                            'deployment_id': f'deployment-{url_index}-{index}',
                            'wire_model_id': f'wire-{url_index}-{index}'})
        for url_index, url in enumerate(urls) for index in range(per_url)]
    monkeypatch.setattr(adapter.RouteCandidateCompiler, 'compile',
                        lambda *args, **kwargs: list(candidates))
    monkeypatch.setattr(adapter.RouteCandidateCompiler, 'compile_fallbacks',
                        lambda *args, **kwargs: [])
    return repository


def test_endpoint_checks_run_once_per_distinct_base_url(pool, monkeypatch):
    from lib.byo import byo_egress
    urls = ['http://10.0.0.1:8000/v1', 'http://10.0.0.2:8000/v1', 'http://10.0.0.3:8000/v1']
    repository = _spread_over_endpoints(monkeypatch, urls, per_url=4)
    validated, probed = [], []
    real_validate = byo_egress.validate_egress_url
    monkeypatch.setattr(byo_egress, 'validate_egress_url',
                        lambda url: validated.append(url) or real_validate(url))
    monkeypatch.setattr(ephemeral, '_preflight_enabled', lambda: True)
    monkeypatch.setattr(ephemeral, '_probe_reachable',
                        lambda url: probed.append(url) or (True, 'ok'))

    group = _group(repository)
    try:
        assert len(group.handles) == 12
        assert sorted(validated) == sorted(probed) == urls
        assert [handle.slot.base_url for handle in group.handles] == [
            url for url in urls for _ in range(4)]
    finally:
        adapter.dispose_routed_slot_group(group)


def test_failed_endpoint_check_skips_only_its_candidates(pool, monkeypatch):
    urls = ['http://10.0.0.1:8000/v1', 'http://10.0.0.2:8000/v1']
    repository = _spread_over_endpoints(monkeypatch, urls, per_url=3)
    probed = []
    monkeypatch.setattr(ephemeral, '_preflight_enabled', lambda: True)

    def probe(url):
        probed.append(url)
        return (url != urls[0], 'connection refused')

    monkeypatch.setattr(ephemeral, '_probe_reachable', probe)
    group = _group(repository)
    try:
        assert probed == urls
        assert [handle.slot.base_url for handle in group.handles] == [urls[1]] * 3
        assert [handle.slot.route_rank for handle in group.handles] == [0, 1, 2]
        assert group.primary.slot.route_snapshot['degradation_reasons']
    finally:
        adapter.dispose_routed_slot_group(group)


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
    from lib import proxy
    url = 'http://127.0.0.1:1/v1'
    before = (proxy.describe_route(url), proxy.proxies_for(url))
    original_prepare = adapter.prepare_ephemeral_slot
    prepared = []
    def prepare(**kwargs):
        if len(prepared) == 3:
            raise failure
        prepared.append(original_prepare(**kwargs))
        return prepared[-1]
    monkeypatch.setattr(adapter, 'prepare_ephemeral_slot', prepare)
    with pytest.raises(type(failure)):
        _group(_repository())
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []
    assert all(handle.disposed and not handle.proxy_bypass_host for handle in prepared)
    assert (proxy.describe_route(url), proxy.proxies_for(url)) == before


def test_group_publication_failure_registers_nothing_and_returns_exemptions(
        pool, monkeypatch):
    from lib import proxy
    url = 'http://127.0.0.1:1/v1'
    before = (proxy.describe_route(url), proxy.proxies_for(url))
    monkeypatch.setattr(ephemeral, '_handles', _RefusingHandles())
    with pytest.raises(RuntimeError, match='registry unavailable'):
        _group(_repository(6))
    assert pool.slots == [] and len(ephemeral._handles) == 0
    assert (proxy.describe_route(url), proxy.proxies_for(url)) == before


def test_group_disposal_failure_before_detach_remains_retryable(pool, monkeypatch):
    group = _group(_repository())
    original_dispose = adapter.dispose_ephemeral_slots
    attempts = []
    def dispose(handles):
        attempts.append(list(handles))
        if len(attempts) == 1:
            raise RuntimeError('temporary disposal failure')
        return original_dispose(handles)
    monkeypatch.setattr(adapter, 'dispose_ephemeral_slots', dispose)
    with pytest.raises(RuntimeError, match='temporary disposal failure'):
        adapter.dispose_routed_slot_group(group)
    assert ephemeral.count_ephemeral_slots() == 64
    assert pool.slots == [handle.slot for handle in group.handles]
    # Plaintext is neutralized even though detachment must be retried.
    assert all(not handle.slot.api_key for handle in group.handles)
    assert adapter.dispose_routed_slot_group(group)
    assert attempts[0] == attempts[1] == group.handles
    assert ephemeral.count_ephemeral_slots() == 0
    assert pool.slots == []


def test_one_sibling_release_failure_still_releases_every_sibling(pool, monkeypatch):
    from lib import proxy
    group = _group(_repository(4))
    real_release = proxy.release_no_proxy_host
    released = []
    def release(host):
        released.append(host)
        if len(released) == 1:
            raise RuntimeError('proxy table busy')
        return real_release(host)
    monkeypatch.setattr(proxy, 'release_no_proxy_host', release)
    try:
        with pytest.raises(RuntimeError, match='proxy table busy'):
            adapter.dispose_routed_slot_group(group)
        assert released == ['127.0.0.1'] * 4
        assert pool.slots == [] and ephemeral.count_ephemeral_slots() == 0
        assert not adapter.dispose_routed_slot_group(group)
        assert group.disposed
    finally:
        real_release('127.0.0.1')  # the lease the injected failure kept


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


class _RefusingHandles(dict):
    def __setitem__(self, key, value):
        raise RuntimeError('registry unavailable')


@pytest.mark.parametrize('stage,url', [
    ('preflight', 'http://10.98.76.54:8000/v1'),
    ('publication', 'http://10.98.76.55:8000/v1'),
    ('handoff', 'http://10.98.76.56:8000/v1'),
])
def test_failed_mint_returns_its_proxy_exemption(pool, monkeypatch, stage, url):
    from lib import proxy
    before = (proxy.describe_route(url), proxy.proxies_for(url))
    if stage == 'preflight':
        monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '1')
        monkeypatch.setattr(ephemeral, '_probe_reachable',
                            lambda _url: (False, 'connection refused'))
        expected = ValueError
    elif stage == 'publication':
        monkeypatch.setattr(ephemeral, '_handles', _RefusingHandles())
        expected = RuntimeError
    else:
        original_audit = ephemeral.audit_log
        def cancel_mint(event, **details):
            if event == 'ephemeral_slot_mint':
                raise asyncio.CancelledError()
            return original_audit(event, **details)
        monkeypatch.setattr(ephemeral, 'audit_log', cancel_mint)
        expected = asyncio.CancelledError
    with pytest.raises(expected):
        ephemeral.mint_ephemeral_slot(base_url=url, api_key='', model_id='test-model')
    assert (proxy.describe_route(url), proxy.proxies_for(url)) == before
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



def test_default_group_retains_every_authorized_candidate_and_full_rank(pool):
    repository = _repository(131)
    group = _group(repository)
    try:
        assert len(group.handles) == len(group.candidates) == 131
        assert {c.deployment['deployment_id'] for c in group.candidates} == {f'deployment-{i}' for i in range(131)}
        assert [h.slot.route_rank for h in group.handles] == list(range(131))
    finally:
        adapter.dispose_routed_slot_group(group)
    assert not pool.slots and ephemeral.count_ephemeral_slots() == 0


def test_planned_route_beyond_old_prefix_matches_before_secret_resolution(pool, monkeypatch):
    from lib.model_routing.routing import compile_candidates
    from lib.model_routing.domain import ModelRoutingError
    repository = _repository(131)
    owner = OwnerBoundary.create(23)
    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a')
    target = compile_candidates(repository.get(owner).document, selection)[100]
    witness = adapter.candidate_route_identity(target, 7)
    group = adapter.mint_routed_slot_group(repository, owner, selection,
        max_candidates=1, expected_route_identity=witness)
    try:
        assert len(group.handles) == 1
        assert group.candidates[0].deployment['deployment_id'] == witness['deploymentId']
    finally:
        adapter.dispose_routed_slot_group(group)
    def forbidden(*args, **kwargs):
        pytest.fail('changed planned witness resolved a secret or minted a handle')
    monkeypatch.setattr(adapter, 'prepare_ephemeral_slot', forbidden)
    repository.resolve_secret = forbidden
    with pytest.raises(ModelRoutingError) as failure:
        adapter.mint_routed_slot_group(repository, owner, selection,
            expected_route_identity={**witness, 'revision': 6})
    assert failure.value.kind == 'model_route_changed'
    assert not pool.slots


def test_explicit_shortlist_above_old_quota_is_honored(pool):
    repository = _repository(131)
    group = adapter.mint_routed_slot_group(repository, OwnerBoundary.create(23),
        NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a'), max_candidates=100)
    try:
        assert len(group.handles) == 100
    finally:
        adapter.dispose_routed_slot_group(group)


def test_all_primary_routes_precede_fallbacks_when_the_primary_set_exceeds_old_quota(pool):
    repository = _repository(70)
    document = repository.get(OwnerBoundary.create(23)).document
    document['deployments'].append(copy.deepcopy(_full_document()['deployments'][-1]))
    document['deployments'][-1]['deployment_id'] = 'late-fallback'
    group = _group(repository)
    try:
        primary = [c for c in group.candidates if c.model['model_id'] == 'alpha']
        fallback = [c for c in group.candidates if c.model['model_id'] == 'beta']
        assert len(primary) == 70 and len(fallback) == 1
        assert group.candidates[:70] == primary
        assert group.candidates[70:] == fallback
    finally:
        adapter.dispose_routed_slot_group(group)


def test_route_group_records_one_audit_entry_per_publication_and_withdrawal(pool, monkeypatch):
    events = []
    monkeypatch.setattr(ephemeral, 'audit_log',
                        lambda event, **details: events.append((event, details)))
    group = _group(_repository(59))
    adapter.dispose_routed_slot_group(group)
    assert [event for event, _ in events] == ['ephemeral_slot_mint', 'ephemeral_slot_dispose']
    minted, disposed = events[0][1], events[1][1]
    assert minted['slots'] == disposed['slots'] == disposed['removed'] == 59
    assert minted['provider_id'] == disposed['provider_id'] == group.pin_id
    assert minted['handle'] == group.handles[0].handle_id
    assert 'api_key' not in minted

"""Selection scans only the slots its execution context may use.

Every pick used to walk the whole process-wide pool under the global
dispatcher lock and read key health for each slot before testing the provider
pin. With about 1,600 live request-scoped slots (one route group of 45-59 per
task) a group's pick cost O(all live slots) plus a store ``stat`` per slot.

Contract:
  * a provider-pinned pick evaluates only its own route group and reads key
    health at most once per group slot; it checks key-store freshness once,
    before taking the dispatcher lock, so no store ``stat`` runs under it;
  * an unpinned selection draws only from the configured pool: a
    request-scoped slot belongs to the request that minted it and never
    serves another caller's unpinned dispatch;
  * direct list mutation of ``dispatcher.slots`` keeps every scope exact;
  * key admission reads each candidate's last-resort siblings from the
    pool's exact credential index, which every publication, withdrawal and
    reload updates at once, never from a walk over every live slot;
  * every pick records its latency for agent and translation threads alike.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import lib.llm_dispatch.api as api
from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.provider_pin import provider_pin
from lib.llm_dispatch.slot import Slot

pytestmark = pytest.mark.unit

_PINS = ('model-route:7:a', 'model-route:7:b', 'model-route:7:c')


def _group_slot(pin, rank, *, latency=100.0, owner=7):
    return Slot(
        key_name=f'ephemeral_{pin[-1]}{rank}', api_key='sk-request',
        model='wire-alpha', logical_model='alpha', capabilities={'text'},
        provider_id=pin, request_scoped=True, routing_provider_id='provider-a',
        routing_owner_user_id=owner, route_credential_id=f'credential-{pin[-1]}{rank}',
        route_deployment_id=f'deployment-{rank}', route_rank=rank,
        latency_ema=latency)


def _configured(index, *, latency=1000.0):
    return Slot(
        key_name=f'configured-{index}', api_key='sk-configured',
        model='wire-alpha', logical_model='alpha', capabilities={'text'},
        provider_id='provider-a', latency_ema=latency)


def _identities(slots):
    return {(slot.key_stats_provider_id(), slot.key_stats_key_name())
            for slot in slots}


@pytest.fixture
def pool(monkeypatch):
    reads = []

    def recording_is_key_enabled(provider_id, key_name, model=''):
        reads.append((provider_id, key_name))
        return True

    monkeypatch.setattr('lib.key_stats.is_key_enabled', recording_is_key_enabled)
    dispatcher = LLMDispatcher()
    configured = [_configured(index) for index in range(20)]
    groups = {pin: [_group_slot(pin, rank) for rank in range(5)] for pin in _PINS}
    dispatcher.slots = configured + [slot for group in groups.values() for slot in group]
    dispatcher._initialized = True
    return SimpleNamespace(dispatcher=dispatcher, configured=configured,
                           groups=groups, reads=reads)


def test_pinned_pick_reads_key_health_only_for_its_own_group(pool):
    own = pool.groups['model-route:7:b']
    with provider_pin('model-route:7:b'):
        chosen = pool.dispatcher.pick_and_reserve(
            capability='text', prefer_model='alpha', strict_model=True)

    assert chosen is own[0]
    assert pool.reads and set(pool.reads) <= _identities(own)
    assert len(pool.reads) == len(set(pool.reads)) <= len(own)


def test_pinned_capacity_probe_reads_only_its_own_group_identity(pool):
    from lib.key_stats import strict_billing_stop_admission

    own = pool.groups['model-route:7:c']
    with provider_pin('model-route:7:c'), strict_billing_stop_admission():
        assert pool.dispatcher.has_capable_slots('text')
    # The same credential-health identity the picker and recorder use.
    assert pool.reads and set(pool.reads) <= _identities(own)




def test_disabled_group_key_is_still_honoured(pool, monkeypatch):
    own = pool.groups['model-route:7:a']
    disabled = own[0].key_stats_key_name()
    monkeypatch.setattr(
        'lib.key_stats.is_key_enabled',
        lambda provider_id, key_name, model='': key_name != disabled)
    with provider_pin('model-route:7:a'):
        assert pool.dispatcher.pick_slot(
            capability='text', prefer_model='alpha', strict_model=True) is own[1]


@pytest.fixture
def key_store(monkeypatch, tmp_path):
    """Real key admission against an isolated store."""
    import lib.key_stats as ks
    from lib.key_stats import _state

    ks.shutdown_key_stats()
    monkeypatch.setattr(_state, 'KEY_STATS_FLUSH_INTERVAL_SECONDS', 3600.0)
    snapshot = {key: ks._cache[key] for key in ('day', 'stats', 'overrides', 'loaded')}
    pending = list(_state._pending_mutations)
    monkeypatch.setattr(ks, '_STATS_PATH', str(tmp_path / 'key_stats.json'))
    ks._cache.update(day='', stats={}, overrides={}, loaded=False)
    _state._pending_mutations.clear()
    yield ks
    _state._pending_mutations.clear()
    ks.shutdown_key_stats()
    ks._cache.update(snapshot)
    _state._pending_mutations[:] = pending


@pytest.fixture
def real_key_stats(key_store, monkeypatch):
    from lib.key_stats import _state

    monkeypatch.setattr(key_store, '_list_siblings', lambda provider: [])
    fingerprints = []
    real_fingerprint = _state._file_fingerprint
    monkeypatch.setattr(_state, '_file_fingerprint',
                        lambda path: fingerprints.append(path) or real_fingerprint(path))
    key_store.is_key_enabled('warm', 'load')
    fingerprints.clear()
    yield fingerprints


def test_pinned_pick_checks_key_store_freshness_once_outside_the_lock(
        real_key_stats, monkeypatch):
    from lib.key_stats import _state
    dispatcher = LLMDispatcher()
    group = [_group_slot(_PINS[0], rank) for rank in range(12)]
    dispatcher.slots = [_configured(index) for index in range(30)] + group
    dispatcher._initialized = True
    held = []
    counting = _state._file_fingerprint
    monkeypatch.setattr(_state, '_file_fingerprint', lambda path: (
        held.append(dispatcher._lock.locked()) or counting(path)))

    with provider_pin(_PINS[0]):
        assert dispatcher.pick_slot(
            capability='text', prefer_model='alpha', strict_model=True) is group[0]
    assert len(real_key_stats) == 1
    assert held == [False]


def test_pinned_pick_never_walks_the_live_pool(key_store, monkeypatch):
    """A pick among about 1,600 live slots reads only its own scope's slots.

    Key admission reads each candidate's last-resort siblings from the pool's
    credential index instead of projecting them from every live slot.
    """
    from lib.llm_dispatch import factory

    dispatcher = LLMDispatcher()
    groups = [[_group_slot(f'model-route:43:{index:02d}', rank, owner=43)
               for rank in range(53)] for index in range(30)]
    dispatcher.slots = [_configured(index) for index in range(10)] + [
        slot for group in groups for slot in group]
    dispatcher._initialized = True
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)
    own = groups[7]
    read = set()
    for name in ('key_stats_provider_id', 'key_stats_key_name'):
        def observed(slot):
            read.add(id(slot))
            return getattr(slot, name)
        monkeypatch.setattr(Slot, name, observed)

    with provider_pin(own[0].provider_id):
        chosen = dispatcher.pick_and_reserve(
            capability='text', prefer_model='alpha', strict_model=True)

    assert len(dispatcher.slots) == 1600
    assert chosen is own[0]
    assert read and read <= {id(slot) for slot in own}


def test_a_pick_reads_siblings_from_its_own_pool_not_the_factory(key_store, monkeypatch):
    """Key admission consults siblings while the pick holds the dispatcher lock.

    Resolving the dispatcher there (``get_dispatcher``) can bind its resource
    port, which samples the dispatcher under that same non-reentrant lock, so
    the pick hands key admission its own pool's credential index.
    """
    from lib.llm_dispatch import factory

    dispatcher = LLMDispatcher()
    group = [_group_slot('model-route:44:a', rank, owner=44) for rank in range(3)]
    dispatcher.slots = [_configured(0)] + group
    dispatcher._initialized = True
    resolved = []
    monkeypatch.setattr(factory, 'get_dispatcher',
                        lambda: resolved.append('get_dispatcher') or dispatcher)

    with provider_pin(group[0].provider_id):
        chosen = dispatcher.pick_and_reserve(
            capability='text', prefer_model='alpha', strict_model=True)

    assert chosen is group[0]
    assert resolved == []


def _credential_slot(pin, credential, model, *, owner=31):
    return Slot(
        key_name=f'ephemeral_{pin[-1]}_{credential}_{model}', api_key='sk-request',
        model=model, logical_model=model, capabilities={'text'},
        provider_id=pin, request_scoped=True, routing_provider_id='provider-s',
        routing_owner_user_id=owner, route_credential_id=credential)


def test_key_health_siblings_follow_route_group_mint_and_withdrawal(monkeypatch):
    """The last-resort guard sees exactly the credentials of live slots.

    Siblings stay owner-scoped per Provider. Several slots share one
    credential, so withdrawing one route group keeps a credential that
    another live group still uses, and withdrawing its last slot removes it.
    """
    import lib.key_stats as ks
    from lib.llm_dispatch import factory

    dispatcher = LLMDispatcher()
    dispatcher._initialized = True
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)
    first = [_credential_slot('model-route:31:a', 'credential-1', model)
             for model in ('m1', 'm2')]
    second = [_credential_slot('model-route:31:b', 'credential-1', 'm3'),
              _credential_slot('model-route:31:b', 'credential-2', 'm1')]
    foreign = [_credential_slot('model-route:32:a', 'credential-1', 'm1', owner=32)]

    def siblings(owner=31):
        namespace = f'owner:{owner}:provider-s'
        return [pair.removeprefix(f'{namespace}::')
                for pair in ks._list_siblings(namespace)]

    def change(operation, slots):
        with dispatcher._lock:
            getattr(dispatcher.slots, operation)(slots)

    assert siblings() == []
    change('publish', first + foreign)
    assert siblings() == ['credential-1']
    assert siblings(32) == ['credential-1']
    change('publish', second)
    assert siblings() == ['credential-1', 'credential-2']
    change('withdraw', first)
    assert siblings() == ['credential-1', 'credential-2']
    change('withdraw', second[:1])
    assert siblings() == ['credential-2']
    change('withdraw', second[1:] + foreign)
    assert siblings() == [] and siblings(32) == []


def test_key_health_siblings_follow_reload_and_list_edits(monkeypatch):
    import lib.key_stats as ks
    from lib.llm_dispatch import factory

    dispatcher = LLMDispatcher()
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: dispatcher)

    def siblings():
        return [pair.removeprefix('provider-a::')
                for pair in ks._list_siblings('provider-a')]

    with dispatcher._lock:
        dispatcher.slots.replace_configured([_configured(0), _configured(1)])
    assert siblings() == ['configured-0', 'configured-1']
    dispatcher.slots.append(_configured(2))
    assert siblings() == ['configured-0', 'configured-1', 'configured-2']
    with dispatcher._lock:
        dispatcher.slots.replace_configured([_configured(1)])
    assert siblings() == ['configured-1']
    dispatcher.slots = []
    assert siblings() == []


def test_unpinned_selection_never_uses_another_requests_slot(monkeypatch):
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    dispatcher = LLMDispatcher()
    foreign = _group_slot('model-route:7:x', 0, latency=1.0)
    configured = _configured(0, latency=5000.0)
    dispatcher.slots = [foreign, configured]
    dispatcher._initialized = True

    for _ in range(10):
        assert dispatcher.pick_slot(capability='text', prefer_model='alpha') is configured
        assert dispatcher.pick_slot(capability='text') is configured
    assert dispatcher.pick_top_n(n=2, reserve=False) == [configured]
    assert dispatcher.pick_best_slots('text') == [configured]

    dispatcher.slots = [foreign]
    assert dispatcher.pick_slot(capability='text', prefer_model='alpha') is None
    assert dispatcher.pick_slot(capability='vision') is None
    assert not dispatcher.has_capable_slots('text')
    assert dispatcher.pick_top_n(n=2, reserve=False) == []
    assert dispatcher.pick_key_for_model('wire-alpha') == ('', '', None)
    with provider_pin('model-route:7:x'):
        assert dispatcher.pick_slot(capability='text', prefer_model='alpha') is foreign


def test_direct_pool_mutations_keep_scopes_exact(monkeypatch):
    monkeypatch.setattr('lib.key_stats.is_key_enabled', lambda *a, **k: True)
    dispatcher = LLMDispatcher()
    dispatcher._initialized = True
    pin = 'model-route:7:m'
    first, second = _group_slot(pin, 0), _group_slot(pin, 1)

    def pinned_pick():
        with provider_pin(pin):
            return dispatcher.pick_slot(capability='text', prefer_model='alpha')

    dispatcher.slots = [first]
    dispatcher.slots.append(second)
    assert pinned_pick() is first
    dispatcher.slots.remove(first)
    assert pinned_pick() is second
    dispatcher.slots[:] = [first]
    assert pinned_pick() is first
    del dispatcher.slots[0]
    assert pinned_pick() is None
    dispatcher.slots += [second]
    assert pinned_pick() is second
    dispatcher.slots.insert(0, first)
    assert pinned_pick() is first
    dispatcher.slots.pop(0)
    assert pinned_pick() is second
    dispatcher.slots.clear()
    assert pinned_pick() is None
    dispatcher.slots.extend([second, first])
    assert pinned_pick() is first
    assert dispatcher.slots == [second, first]


def test_slot_summary_lists_only_the_callers_scope(pool):
    own = pool.groups['model-route:7:a']
    with provider_pin('model-route:7:a'):
        summary = pool.dispatcher.summarize_slots('text')
    assert all(slot.key_name in summary for slot in own)
    foreign = pool.configured + pool.groups['model-route:7:b']
    assert not any(slot.key_name in summary for slot in foreign)


def test_every_pick_records_route_selection_latency(pool, monkeypatch):
    observed = []
    monkeypatch.setattr(
        'lib.observability.record_route_selection',
        lambda seconds, *, scope, outcome: observed.append((scope, outcome, seconds)))
    with provider_pin('model-route:7:a'):
        pool.dispatcher.pick_and_reserve(
            capability='text', prefer_model='alpha', strict_model=True)
    pool.dispatcher.pick_slot(
        capability='text', prefer_model='no-such-model', strict_model=True)

    assert [(scope, outcome) for scope, outcome, _ in observed] == [
        ('pinned', 'selected'), ('shared', 'none')]
    assert all(seconds >= 0 for *_, seconds in observed)


@pytest.mark.parametrize('path', ['stream', 'async_stream', 'chat'])
def test_chat_dispatch_pick_emits_the_latency_record(pool, monkeypatch, path):
    """A chat dispatch records every pick and reports the dispatch's pick time.

    The per-pick metric is the lossless record (no log budget applies to
    it); the dispatch evidence carries the summed pick time to the existing
    per-round completion line instead of adding a per-pick log line.
    """
    import asyncio
    observed = []
    monkeypatch.setattr(
        'lib.observability.record_route_selection',
        lambda seconds, *, scope, outcome: observed.append((scope, outcome, seconds)))
    monkeypatch.setattr(api, 'get_dispatcher', lambda: pool.dispatcher)
    messages = [{'role': 'user', 'content': 'hi'}]
    usage = {'prompt_tokens': 1, 'completion_tokens': 1}
    with provider_pin('model-route:7:a'):
        if path == 'stream':
            monkeypatch.setattr('lib.llm.stream_chat',
                                lambda body, **kwargs: ('ok', 'stop', dict(usage)))
            result = api.dispatch_stream(messages, prefer_model='alpha', strict_model=True,
                                         log_prefix='[T]', max_retries=1)[2]
        elif path == 'async_stream':
            async def stream(body, **kwargs):
                return 'ok', 'stop', dict(usage)
            monkeypatch.setattr('lib.llm.astream.async_stream_chat', stream)
            result = asyncio.run(api.async_dispatch_stream(
                messages, prefer_model='alpha', strict_model=True,
                log_prefix='[T]', max_retries=1))[2]
        else:
            monkeypatch.setattr('lib.llm.chat', lambda *args, **kwargs: ('ok', dict(usage)))
            result = api.dispatch_chat(messages, prefer_model='alpha', strict_model=True,
                                       log_prefix='[T]', max_retries=1)[1]

    assert [(scope, outcome) for scope, outcome, _ in observed] == [('pinned', 'selected')]
    evidence = result['_dispatch']
    # The dispatch times the whole pick call, so it covers the pick's own clock.
    assert evidence['route_selection_ms'] >= round(observed[0][2] * 1000, 3)
    assert evidence['route_selection_ms'] < 60_000


def test_round_completion_line_reports_the_dispatch_pick_time(pool, monkeypatch, caplog):
    from lib.tasks_pkg.manager._stream import _log_stream_completion
    monkeypatch.setattr(api, 'get_dispatcher', lambda: pool.dispatcher)
    monkeypatch.setattr('lib.llm.stream_chat', lambda body, **kwargs: (
        'ok', 'stop', {'prompt_tokens': 1, 'completion_tokens': 1}))
    with provider_pin('model-route:7:a'):
        usage = api.dispatch_stream([{'role': 'user', 'content': 'hi'}], prefer_model='alpha',
                                    strict_model=True, log_prefix='[T]', max_retries=1)[2]
    caplog.set_level(logging.INFO)
    message = {'content': 'ok', 'tool_calls': []}
    _log_stream_completion({'convId': 'c1'}, prefix='[T]', model='alpha',
                           finish_reason='stop', message=message, dispatch=usage['_dispatch'])
    _log_stream_completion({'convId': 'c1'}, prefix='[T]', model='alpha',
                           finish_reason='stop', message=message, dispatch={})
    lines = [record.getMessage() for record in caplog.records
             if 'stream_llm_response complete' in record.getMessage()]
    assert lines[0].endswith(f"route_selection_ms={usage['_dispatch']['route_selection_ms']}")
    assert lines[1].endswith('route_selection_ms=-')


def test_route_selection_metric_is_exposed_with_bounded_labels():
    from lib import observability

    observability.record_route_selection(0.002, scope='pinned', outcome='selected')
    observability.record_route_selection(0.002, scope='request-1234', outcome='?')
    lines = '\n'.join(observability.prometheus_lines())
    assert 'tofu_llm_route_selection_seconds_bucket{' in lines
    assert 'scope="pinned"' in lines and 'request-1234' not in lines


def test_stream_attempt_summarizes_the_pool_only_for_debug_logging(
        pool, monkeypatch, caplog):
    summaries = []
    monkeypatch.setattr(pool.dispatcher, 'summarize_slots',
                        lambda capability=None: summaries.append(capability) or '')
    monkeypatch.setattr(api, 'get_dispatcher', lambda: pool.dispatcher)
    monkeypatch.setattr(
        'lib.llm.stream_chat',
        lambda body, **kwargs: ('ok', 'stop', {'prompt_tokens': 1, 'completion_tokens': 1}))

    def dispatch():
        with provider_pin('model-route:7:a'):
            return api.dispatch_stream(
                [{'role': 'user', 'content': 'hi'}], prefer_model='alpha',
                strict_model=True, log_prefix='[T]', max_retries=1)

    caplog.set_level(logging.INFO, logger='lib.llm_dispatch.api')
    assert dispatch()[0] == 'ok'
    assert summaries == []

    caplog.set_level(logging.DEBUG, logger='lib.llm_dispatch.api')
    assert dispatch()[0] == 'ok'
    assert summaries

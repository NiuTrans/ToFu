"""Route order and ancestry reflect admitted provider attempts, never ranks."""
import copy

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def routed_group(monkeypatch):
    from tests.llm_dispatch.test_dispatcher_model_routing_v2 import _build, _document
    from lib.model_routing import (
        InMemoryModelRoutingRepository, OwnerBoundary,
        mint_routed_slot_group, dispose_routed_slot_group,
        parse_native_model_selection,
    )
    document = _document()
    for connection in document['connections']:
        connection['base_url'] = 'http://127.0.0.1:19876/v1'
    dispatcher = _build(monkeypatch, document)
    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: dispatcher)
    monkeypatch.setattr('lib.llm_dispatch.ephemeral._preflight_enabled', lambda: False)
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    repository.compare_and_swap(boundary, document, expected_revision=0)
    group = mint_routed_slot_group(repository, boundary, parse_native_model_selection({
        'model': {'creator_id': 'creator', 'model_id': 'alpha'},
        'routing': {'preferred_provider_id': 'provider-a'},
    }))
    try:
        yield dispatcher, group
    finally:
        dispose_routed_slot_group(group)


def test_candidate_rank_is_not_a_completed_failover(routed_group):
    _, group = routed_group
    assert len(group.handles) == 2
    assert [handle.slot.context_window for handle in group.handles] == [100_000, 80_000]
    for handle in group.handles:
        assert all(t['kind'] == 'initial'
                   for t in handle.slot.route_snapshot['transitions'])


def test_compiler_order_survives_score_jitter_and_honors_exclusions(
        routed_group, monkeypatch):
    from lib.llm_dispatch.provider_pin import provider_pin
    dispatcher, group = routed_group
    primary, backup = [h.slot for h in group.handles]
    monkeypatch.setattr(primary, 'score', lambda: 1050.0)
    monkeypatch.setattr(backup, 'score', lambda: 950.0)
    with provider_pin(group.pin_id):
        first = dispatcher.pick_and_reserve(capability='text', prefer_model='alpha')
        assert first is primary
        first.release()
        second = dispatcher.pick_and_reserve(
            capability='text', prefer_model='alpha',
            exclude_pairs={(primary.key_name, primary.model)})
        assert second is backup
        second.release()


@pytest.mark.parametrize('mode', ['sync', 'async', 'chat'])
@pytest.mark.parametrize('rotate', [False, True])
def test_dispatch_snapshot_uses_actual_attempts(routed_group, monkeypatch, mode, rotate):
    import asyncio
    import lib.llm as llm
    from lib.llm_dispatch import api
    from lib.llm_dispatch.provider_pin import provider_pin
    from lib.llm.llm_errors import PermissionError_
    from lib.llm.reasoning import capture_intent
    dispatcher, group = routed_group
    monkeypatch.setattr(api, 'get_dispatcher', lambda: dispatcher)
    primary, backup = [h.slot for h in group.handles]
    originals = [copy.deepcopy(h.slot.route_snapshot) for h in group.handles]
    monkeypatch.setattr(primary, 'score', lambda: 950.0)
    monkeypatch.setattr(backup, 'score', lambda: 1050.0)
    calls = []

    def transport(*args, **kwargs):
        calls.append(kwargs)
        if rotate and len(calls) == 1:
            raise PermissionError_('synthetic denied route', status_code=401)
        if mode == 'chat':
            return 'ok', {}
        return {'role': 'assistant', 'content': 'ok'}, 'stop', {}

    async def async_transport(*args, **kwargs):
        return transport(*args, **kwargs)

    monkeypatch.setattr(llm, 'stream_chat', transport)
    monkeypatch.setattr('lib.llm.astream.async_stream_chat', async_transport)
    monkeypatch.setattr(llm, 'chat', transport)
    body = {'messages': [{'role': 'user', 'content': 'synthetic'}],
            '_reasoning_intent': capture_intent(thinking_depth='default')}
    with provider_pin(group.pin_id):
        if mode == 'chat':
            _, usage = api.dispatch_chat(body['messages'], prefer_model='alpha')
        elif mode == 'async':
            _, _, usage = asyncio.run(api.async_dispatch_stream(body, prefer_model='alpha'))
        else:
            _, _, usage = api.dispatch_stream(body, prefer_model='alpha')
    snapshot = usage['_dispatch']['route_snapshot']
    assert len(calls) == (2 if rotate else 1)
    assert snapshot['provider_id'] == ('provider-b' if rotate else 'provider-a')
    transitions = snapshot['transitions']
    assert [t['kind'] for t in transitions] == (
        ['initial', 'provider_failover'] if rotate else ['initial'])
    assert transitions[0]['from'] is None
    assert transitions[0]['to']['provider_id'] == 'provider-a'
    if rotate:
        assert transitions[1]['from']['provider_id'] == 'provider-a'
        assert transitions[1]['to']['provider_id'] == 'provider-b'
    assert [h.slot.route_snapshot for h in group.handles] == originals
    assert all(h.slot.inflight == 0 for h in group.handles)


def test_skipped_candidates_and_previous_calls_do_not_create_ancestry(routed_group):
    from lib.llm_dispatch._route_trace import RouteAttemptTrace
    _, group = routed_group
    backup = group.handles[1].slot
    for _ in range(2):
        trace = RouteAttemptTrace()
        trace.start(backup)
        snapshot = trace.snapshot()
        assert snapshot['transitions'] == [{
            'kind': 'initial', 'from': None,
            'to': {'provider_id': 'provider-b', 'offering_id': 'alpha-b',
                   'deployment_id': 'alpha-deployment-b'},
            'reason': 'computed_v2_route',
        }]


def test_route_cause_is_bounded_and_only_on_real_transitions(routed_group):
    """``cause`` answers WHY the route changed: a failure class when the
    previous slot died, or ``route_rescore`` when nothing failed. It must
    never appear on the initial route, and a fresh failure cause must not
    bleed into a later unrelated transition."""
    from lib.llm_dispatch._route_trace import RouteAttemptTrace
    _, group = routed_group
    primary, backup = group.handles[0].slot, group.handles[1].slot

    # Failure-driven failover carries the exception class name.
    trace = RouteAttemptTrace()
    trace.start(primary)
    assert 'cause' not in trace.snapshot()['transitions'][0]
    trace.note_cause('EndpointUnreachableError')
    trace.start(backup)
    t = trace.snapshot()['transitions'][-1]
    assert t['cause'] == 'EndpointUnreachableError'

    # The cause is consumed by the transition that follows it — a later
    # rescore on a healthy slot does not re-report the stale failure.
    trace.note_cause('route_rescore')
    trace.start(primary)
    t2 = trace.snapshot()['transitions'][-1]
    assert t2['cause'] == 'route_rescore'

    # Free-form text is bounded (defence against a future caller passing an
    # exception message instead of a class name).
    trace2 = RouteAttemptTrace()
    trace2.start(primary)
    trace2.note_cause('x' * 500)
    trace2.start(backup)
    assert len(trace2.snapshot()['transitions'][-1]['cause']) == 120


def test_attempt_lineage_is_bounded_and_distinguishes_model_changes(routed_group):
    import json
    from lib.llm_dispatch._route_trace import RouteAttemptTrace
    from lib.model_routing.domain import MAX_ROUTE_SNAPSHOT_BYTES
    _, group = routed_group
    primary, backup = [h.slot for h in group.handles]
    backup.route_snapshot['actual_model'] = {'creator_id': 'creator', 'model_id': 'beta'}
    original = copy.deepcopy(backup.route_snapshot)
    trace = RouteAttemptTrace()
    for _ in range(100):
        trace.start(primary)
        trace.start(backup)
    snapshot = trace.snapshot()
    assert len(snapshot['transitions']) <= 32
    assert all(t['kind'] == 'model_fallback' for t in snapshot['transitions'])
    assert snapshot['transitions'][-1]['to']['provider_id'] == 'provider-b'
    assert len(json.dumps(snapshot, ensure_ascii=False).encode()) <= MAX_ROUTE_SNAPSHOT_BYTES
    assert backup.route_snapshot == original
    snapshot['transitions'].clear()
    assert trace.snapshot()['transitions']

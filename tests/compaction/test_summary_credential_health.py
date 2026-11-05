"""Learned credential outages steer later auxiliary summary selection.

The request-local ``excluded_routes`` list forgets an outage when the summary
returns. The owner route-health registry keeps it for a bounded cooldown so
the next compaction skips the broken credential instead of rediscovering it.
Dispatch learns that fact exactly once per outage; the summary only consumes it.
"""
from types import SimpleNamespace

import pytest

from lib.llm_dispatch.provider_pin import provider_pin
from lib.model_routing.health import (
    CREDENTIAL_SOURCE_UNAVAILABLE,
    HealthTarget,
    classify_failure,
    existing_owner_health_registry,
    owner_health_registry,
)
from lib.tasks_pkg.compaction._auxiliary_route import record_auxiliary_route_success
from lib.tasks_pkg.compaction._summary_routing import select_summary_route
from tests.compaction import test_summary_credential_resilience as resilience
from tests.compaction.test_auxiliary_summary_integration import native as native  # noqa: F401
from tests.compaction.test_summary_credential_resilience import resilient as resilient  # noqa: F401

pytestmark = pytest.mark.unit

OWNER, TENANT = 41, 'tenant-a'


def _select(native):
    return select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)


def _cool(owner, tenant, credential_id, clock=None):
    health = owner_health_registry(owner, tenant)
    if clock is not None:
        health._clock = clock
    health.record_failure(
        HealthTarget(classify_failure(kind=CREDENTIAL_SOURCE_UNAVAILABLE), credential_id),
        reason=CREDENTIAL_SOURCE_UNAVAILABLE)


def test_summary_selection_skips_cooled_credential_until_cooldown_expires(native):
    first = _select(native)
    assert first['requestOwned'], first
    credential_id = first['routeIdentity']['credentialId']

    _cool(OWNER, 'tenant-b', credential_id)
    _cool(OWNER + 1, TENANT, credential_id)
    assert _select(native)['routeIdentity'] == first['routeIdentity'], (
        'another owner or tenant never steers this owner')

    now = [1000.0]
    _cool(OWNER, TENANT, credential_id, clock=lambda: now[0])
    cooled = _select(native)
    assert (cooled.get('routeIdentity') or {}).get('credentialId') != credential_id, cooled

    now[0] += 301
    assert _select(native)['routeIdentity'] == first['routeIdentity']


def _candidate(credential_id, deployment_id):
    return SimpleNamespace(credential={'credential_id': credential_id},
                           deployment={'deployment_id': deployment_id},
                           connection={'connection_id': 'conn-a'})


def test_auxiliary_success_clears_every_group_credential():
    group = SimpleNamespace(candidates=[_candidate('cred-a', 'dep-a'),
                                        _candidate('cred-b', 'dep-b')])
    targets = [HealthTarget('credential', 'cred-a'), HealthTarget('credential', 'cred-b')]
    for target in targets:
        _cool(OWNER, TENANT, target.entity_id)
    health = existing_owner_health_registry(OWNER, TENANT)
    assert all(health.target_unavailable(target) for target in targets)

    record_auxiliary_route_success(group, owner=OWNER, tenant='tenant-b')
    assert all(health.target_unavailable(target) for target in targets)
    record_auxiliary_route_success(group, owner=OWNER, tenant=TENANT)
    assert not any(health.target_unavailable(target) for target in targets)


@pytest.mark.parametrize(('failure', 'first_attempts', 'reason'), [
    (lambda: resilience._error('codex_home_unavailable'),
     ['provider-a'] * 3 + ['provider-b'], 'codex_home_unavailable'),
    (resilience._sign_in_error, ['provider-a', 'provider-b'], 'sign_in_required'),
], ids=['credential-source', 'sign-in'])
def test_learned_summary_outage_routes_the_next_summary_around_it(
        resilient, monkeypatch, failure, first_attempts, reason):
    attempts = []
    _real_dispatch(resilient, monkeypatch, _codex_outage_transport(
        resilient, attempts, failure=failure))

    def summarize(source):
        with provider_pin(resilience._PARENT_PIN):
            return resilient.summary._generate_query_aware_summary(
                source, 'continue', task=resilient.task, usage_out={})

    assert summarize(resilient.source)
    assert attempts == first_attempts
    [entry] = existing_owner_health_registry(OWNER, TENANT).snapshot()['entries']
    assert (entry['scope'], entry['reason']) == (
        'credential', f'credential_source_unavailable:{reason}')

    attempts.clear()
    later = [*resilient.source, {'role': 'user', 'content': 'A later turn adds context.'}]
    assert summarize(later)
    assert attempts == ['provider-b'], 'the next summary skips the learned outage'


def test_quoted_summary_sign_in_failure_moves_the_next_quote(resilient, monkeypatch):
    """A proactive quote never reselects; the learned fact moves the next quote.

    Optional working-set compaction is priced against one quoted route, so a
    failure inside that attempt keeps the source intact instead of buying a
    different model. Without the learned fact the signed-out login stayed
    the cheapest authorized route and every later quote failed identically.
    """
    from lib.tasks_pkg.compaction._constants import (
        _SUMMARY_DISPATCH_MAX_TOKENS, _SUMMARY_MAX_TOKENS)
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    attempts = []
    _real_dispatch(resilient, monkeypatch, _codex_outage_transport(
        resilient, attempts, failure=resilience._sign_in_error))

    def quoted_summary(prompt):
        # The fixture renders the whole summary prompt as ``prompt``; quote it
        # exactly, as the compactor's preflight projection does.
        monkeypatch.setattr(resilient.summary, '_build_summary_user_content', lambda **_: prompt)
        tokens = _estimate_total_tokens([
            {'role': 'system', 'content': resilient.summary._SUMMARY_SYSTEM_PROMPT},
            {'role': 'user', 'content': prompt}])
        with provider_pin(resilience._PARENT_PIN):
            route = select_summary_route(resilient.task, tokens, _SUMMARY_MAX_TOKENS,
                                         capacity_output_tokens=_SUMMARY_DISPATCH_MAX_TOKENS)
            return route, resilient.summary._generate_query_aware_summary(
                resilient.source, 'continue', task=resilient.task, usage_out={},
                expected_route=route)

    first, result = quoted_summary('fixed input')
    assert first['providerId'] == 'provider-a', first
    assert result is None
    assert attempts == ['provider-a'], 'a quoted optional summary never reselects'
    [entry] = existing_owner_health_registry(OWNER, TENANT).snapshot()['entries']
    assert (entry['scope'], entry['reason']) == (
        'credential', 'credential_source_unavailable:sign_in_required')

    # A later compaction folds more history, so it is a new request rather
    # than a repeat the summary budget suppresses.
    attempts.clear()
    requote, result = quoted_summary('fixed input after a later turn')
    assert requote['providerId'] == 'provider-b', 'the next quote skips the signed-out login'
    assert result == '### Objective\nPreserve the authorized work.'
    assert attempts == ['provider-b']


def test_one_summary_credential_outage_is_learned_once(resilient, monkeypatch):
    """One proven-unsent outage is one learned failure: a 300 s first cooldown.

    The real path meets it on every credential-recovery attempt (each
    re-admits the demoted slot) and the summary loop recorded it again, so a
    single outage escalated straight to failures=4 (2400 s).
    """
    health = owner_health_registry(OWNER, TENANT)
    health._clock = lambda: 1000.0
    calls = []
    _real_dispatch(resilient, monkeypatch, _codex_outage_transport(resilient, calls))

    with provider_pin(resilience._PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out={})

    assert calls == ['provider-a'] * 3 + ['provider-b'], 'three same-source recovery attempts'
    [entry] = health.snapshot()['entries']
    assert (entry['scope'], entry['failures']) == ('credential', 1)
    assert entry['cooldown_remaining'] == pytest.approx(300.0)


def _real_dispatch(resilient, monkeypatch, transport):
    """Keep the real dispatcher, picker and credential-outage routing.

    Only the provider transport and unrelated host waits are synthetic.
    """
    import lib.llm as llm
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch import _api_stream

    resilient.pool.slots = [resilient.parent]
    monkeypatch.setattr(dispatch, 'dispatch_stream', _api_stream.dispatch_stream)
    monkeypatch.setattr(_api_stream, 'get_dispatcher', lambda: resilient.pool)
    monkeypatch.setattr(_api_stream, '_sleep_and_record_queue_wait', lambda *a, **k: None)
    monkeypatch.setattr('lib.llm_dispatch.cache_settle.settle_before_send', lambda *a, **k: None)
    monkeypatch.setattr(resilient.pool, 'reserve_shared_contention_probe',
                        lambda *a, **k: SimpleNamespace(delay_s=0.0, admitted=True))
    monkeypatch.setattr(llm, 'stream_chat', transport)


def _codex_outage_transport(resilient, calls, *, codex_fails=True, failure=None):
    def transport(body, **kwargs):
        slot = resilience._active(resilient)
        calls.append(slot.routing_provider_id)
        if codex_fails and slot.oauth == 'codex':
            raise (failure or (lambda: resilience._error('codex_home_unavailable')))()
        return resilience._complete(slot)
    return transport


@pytest.mark.parametrize('planned_credential', ['repaired', 'still_broken'])
def test_credential_cooled_between_selection_and_mint_keeps_the_plan(
        resilient, monkeypatch, planned_credential):
    """Selection owns learned health; the minted witness is authoritative.

    A concurrent request may cool the selected credential after selection.
    A second health filter at mint raised 'planned route changed' outside the
    outage handling, so the summary failed instead of trying its plan
    (demoted, never stranded) or requalifying after the plan's own failure.
    """
    from lib.tasks_pkg.compaction import _summary_routing as routing

    real_select = routing.select_summary_route
    selections, calls = [], []

    def select_then_concurrent_outage(*args, **kwargs):
        route = real_select(*args, **kwargs)
        selections.append(route)
        if len(selections) == 1:
            _cool(OWNER, TENANT, route['routeIdentity']['credentialId'])
        return route

    monkeypatch.setattr(routing, 'select_summary_route', select_then_concurrent_outage)
    _real_dispatch(resilient, monkeypatch, _codex_outage_transport(
        resilient, calls, codex_fails=planned_credential == 'still_broken'))
    usage = {}
    with provider_pin(resilience._PARENT_PIN):
        result = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)

    assert result is not None, usage.get('_summaryAttempts')
    assert selections[0]['providerId'] == 'provider-a'
    if planned_credential == 'repaired':
        assert calls == ['provider-a']
        assert not existing_owner_health_registry(OWNER, TENANT).target_unavailable(
            HealthTarget('credential', selections[0]['routeIdentity']['credentialId']))
    else:
        assert calls == ['provider-a'] * 3 + ['provider-b']
        assert [row['status'] for row in usage['_summaryAttempts']] == ['failed', 'succeeded']
    assert resilient.pool.slots == [resilient.parent]

"""Recorded billing stops steer the optional auxiliary summary route.

Dispatch records a provider 402 (key-wide) or quota 429 (per wire model) in
key-stats, the durable billing authority. An owner-wide auxiliary summary is
optional work: selection skips a credential its strict optional-work admission
refuses, and its dispatch never challenges a recorded stop. The task's own
route keeps the attended user-supremacy contract. Only provider transport is
synthetic; the owner compiler, private mint, dispatcher and key admission are real.
"""
import pytest

from lib.key_stats import is_strict_billing_stop_admission
from lib.llm.llm_errors import RateLimitError
from lib.llm.stream_result import ProviderStreamResult
from lib.llm_dispatch.provider_pin import provider_pin
from lib.tasks_pkg.compaction._summary_routing import select_summary_route
from tests.compaction import test_summary_credential_resilience as resilience
from tests.compaction.test_auxiliary_summary_integration import native as native  # noqa: F401
from tests.compaction.test_summary_credential_health import _real_dispatch
from tests.compaction.test_summary_credential_resilience import resilient as resilient  # noqa: F401

pytestmark = pytest.mark.unit

# Key-stats identity of the paid leader's minted slot: owner+Provider
# namespace and Credential; per-model stops name the Deployment wire model.
PAID_KEY = ('owner:41:provider-b', 'credential-b')
PAID_WIRE = 'alpha-wire-b'
RESOURCE_KEY = ('owner:41:provider-a', 'credential-a')
SUMMARY = '### Objective\nPreserve the authorized work.'


@pytest.fixture
def billing(monkeypatch, tmp_path):
    """Real key admission against an isolated store and the live slot index."""
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
def paid(resilient):
    # Provider-b is an organization-billed paid API again, so it leads the
    # owner's provider-a subscription resource.
    resilient.state.document['credentials'][1]['kind'] = 'api_key'
    return resilient


def _plan(native):
    with provider_pin(resilience._PARENT_PIN):
        return select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)


def _summarize(native, source):
    usage = {}
    with provider_pin(resilience._PARENT_PIN):
        result = native.summary._generate_query_aware_summary(
            source, 'continue', task=native.task, usage_out=usage)
    return result, usage


def _billing_dead_transport(native, attempts):
    def transport(body, **kwargs):
        slot = resilience._active(native)
        attempts.append(slot.routing_provider_id)
        if slot.routing_provider_id == 'provider-b':
            raise RateLimitError('HTTP 402: insufficient account balance',
                                 is_quota=True, status_code=402)
        return resilience._complete(slot)
    return transport


@pytest.mark.parametrize('stop', ['account_402', 'model_quota', 'stale_manual_on'])
def test_selection_skips_a_billing_stopped_credential(paid, billing, stop):
    first = _plan(paid)
    assert (first['providerId'], first['routeIdentity']['credentialId']) == (
        'provider-b', 'credential-b'), first

    if stop == 'stale_manual_on':
        billing.set_key_override(*PAID_KEY, True)
    billing.mark_key_exhausted(*PAID_KEY, reason='HTTP 402 insufficient balance',
                               model=PAID_WIRE if stop == 'model_quota' else '')

    later = _plan(paid)
    assert later.get('requestOwned') and later['providerId'] == 'provider-a', later
    assert later['_requestRoute']['routeIdentity'] == first['routeIdentity'], (
        'a stop changes the plan, never the summary request identity')


def test_other_model_stop_keeps_and_re_enable_restores_the_credential(paid, billing):
    billing.mark_key_exhausted(*PAID_KEY, reason='insufficient_quota', model='another-wire')
    assert _plan(paid)['providerId'] == 'provider-b', 'a stop names its own model only'

    billing.mark_key_exhausted(*PAID_KEY, reason='HTTP 402 insufficient balance')
    assert _plan(paid)['providerId'] == 'provider-a'
    billing.set_key_override(*PAID_KEY, True)  # The owner topped up and re-enabled it.
    assert _plan(paid)['providerId'] == 'provider-b'


def test_a_wholly_stopped_auxiliary_supply_leaves_the_main_route(paid, billing):
    first = _plan(paid)
    for key in (PAID_KEY, RESOURCE_KEY):
        billing.mark_key_exhausted(*key, reason='HTTP 402 insufficient balance')

    plan = _plan(paid)
    assert not plan.get('requestOwned'), plan
    assert (plan['modelId'], plan['providerId']) == ('gpt-6.1-sol', resilience._PARENT_PIN)
    assert plan['_requestRoute']['routeIdentity'] == first['routeIdentity']


def test_a_402_fails_one_summary_then_later_summaries_route_around_it(
        paid, billing, monkeypatch):
    """A billing-dead paid route used to fail every later compaction.

    Dispatch recorded the 402 as a key-wide stop, but selection never read
    key-stats and ordinary admission promoted the sole credential as last
    resort, so each compaction re-sent the dead route.
    """
    from lib.tasks_pkg.compaction._layer2._prompt import _build_summary_user_content

    # Render the real summary input: a later compaction over a longer
    # transcript is a new request, not the failed one its budget key dedupes.
    monkeypatch.setattr(paid.summary, '_build_summary_user_content',
                        _build_summary_user_content)
    attempts = []
    _real_dispatch(paid, monkeypatch, _billing_dead_transport(paid, attempts))

    result, _ = _summarize(paid, paid.source)
    assert result is None, 'observed provider work never buys a second summary'
    assert attempts == ['provider-b']
    assert billing.get_today_stats(*PAID_KEY)['exhausted'] is True

    attempts.clear()
    later = [*paid.source, {'role': 'user', 'content': 'A later turn adds context.'}]
    result, usage = _summarize(paid, later)
    assert attempts == ['provider-a'], 'the stopped credential is never re-sent'
    assert result == SUMMARY
    assert usage['_summaryRoute']['providerId'] == 'provider-a'
    assert paid.pool.slots == [paid.parent]


def test_stop_recorded_after_selection_is_refused_before_transport(
        paid, billing, monkeypatch):
    """Selection and auxiliary dispatch apply one optional-work admission.

    A concurrent request may record the stop between selection and dispatch;
    ordinary admission would promote the sole credential as last resort.
    """
    from lib.tasks_pkg.compaction import _summary_routing as routing

    real_select = routing.select_summary_route

    def select_then_concurrent_stop(*args, **kwargs):
        route = real_select(*args, **kwargs)
        billing.mark_key_exhausted(*PAID_KEY, reason='HTTP 402 insufficient balance')
        return route

    monkeypatch.setattr(routing, 'select_summary_route', select_then_concurrent_stop)
    attempts = []
    _real_dispatch(paid, monkeypatch, _billing_dead_transport(paid, attempts))

    result, usage = _summarize(paid, paid.source)
    assert result is None
    assert usage['_summaryRoute']['providerId'] == 'provider-b'
    assert attempts == [], 'no upstream request challenges a recorded stop'
    assert paid.pool.slots == [paid.parent]


@pytest.mark.parametrize('policy', ['aa-cheapest', 'main'])
def test_only_the_auxiliary_route_uses_optional_work_admission(native, monkeypatch, policy):
    import lib.llm_dispatch as dispatch

    native.task['config']['compaction']['summaryPolicy'] = policy
    strict = []

    def stream(*args, **kwargs):
        strict.append(is_strict_billing_stop_admission())
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': SUMMARY}, 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 10, '_dispatch': {}})

    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    with provider_pin(resilience._PARENT_PIN):
        result = native.summary._generate_query_aware_summary(
            native.source, 'continue', task=native.task, usage_out={},
            on_delta=lambda chunk: None)

    assert result == SUMMARY
    assert strict == [policy == 'aa-cheapest'], 'the task route keeps user supremacy'
    assert is_strict_billing_stop_admission() is False

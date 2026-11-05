"""Credential-source outages cannot deadlock independently authorized summaries.

The native fixture uses the real owner compiler, private slot mint and disposal;
only provider transport is synthetic. Identical cases run before/after the fix.
"""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from lib.llm.llm_errors import (
    LocalRequestPreparationError, SubscriptionCredentialUnavailableError, SubscriptionLoginRequiredError)
from lib.llm.stream_result import ProviderStreamResult
from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
from tests.compaction.test_auxiliary_summary_integration import native as native

pytestmark = pytest.mark.unit
_PARENT_PIN = 'model-route:41:task-a'


@pytest.fixture
def resilient(native, monkeypatch):
    from lib.tasks_pkg.llm_fallback import _credential_recovery

    native.task.update(id='summary-resilience-task', convId='summary-resilience-conversation', aborted=False)
    # Affordable paid APIs outrank owner resources, so a Codex login is the first
    # summary route only among resources. Provider-b is an independent owner
    # resource (API-key envelope) that requalification may reach.
    native.state.document['credentials'][1]['kind'] = 'subscription'
    clock = [0.0]
    monkeypatch.setattr(_credential_recovery, 'time', SimpleNamespace(
        monotonic=lambda: clock[0],
        sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds)))
    return native


def _active(native):
    slots = [slot for slot in native.pool.slots if slot.provider_id == get_pinned_provider()]
    assert len(slots) == 1
    return slots[0]


def _error(reason='codex_command_unavailable', *, transient=True):
    cause = SubscriptionCredentialUnavailableError(
        reason=reason, credential_source='local', transient=transient)
    error = LocalRequestPreparationError('PRIVATE-PREPARATION-PAYLOAD')
    error.__cause__ = cause
    return error


def _sign_in_error():
    error = LocalRequestPreparationError('PRIVATE-PREPARATION-PAYLOAD')
    error.__cause__ = SubscriptionLoginRequiredError('Codex subscription not logged in')
    return error


def _complete(slot):
    return ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': '### Objective\nPreserve the authorized work.'},
        'stop', {'prompt_tokens': 20, 'completion_tokens': 10,
                 '_dispatch': {'model': slot.logical_model, 'provider_id': slot.routing_provider_id}})


@pytest.mark.parametrize('reason', ['codex_command_unavailable', 'codex_home_unavailable'])
def test_credential_outage_reselects_qualified_api_without_parent_mutation(resilient, monkeypatch, reason):
    source = deepcopy(resilient.source)
    model = resilient.task['model']
    slots = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        slots.append(slot)
        assert kwargs['strict_model'] is True
        assert len(resilient.pool.slots) == 2
        if slot.oauth == 'codex':
            raise _error(reason)
        assert all(old.api_key == '' for old in slots if old.oauth == 'codex')
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        actual = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    assert actual == '### Objective\nPreserve the authorized work.'
    assert [slot.routing_provider_id for slot in slots] == ['provider-a'] * 3 + ['provider-b']
    assert resilient.task['model'] == model and resilient.source == source
    assert resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in slots)
    assert [row['status'] for row in usage['_summaryAttempts']] == ['failed', 'succeeded']
    assert usage['_summaryAttempts'][0]['providerReached'] is False
    assert len(usage['_summaryCalls']) == 1
    assert '_summaryFailure' not in usage


@pytest.mark.parametrize('change', ['same_credential', 'same_secret_reference'])
def test_reselection_skips_every_model_using_failed_credential_source(resilient, monkeypatch, change):
    offering = deepcopy(resilient.state.document['offerings'][2])
    offering.update(offering_id='other-resource-model', model={'creator_id': 'creator', 'model_id': 'gpt-6.1-sol'})
    resilient.state.document['offerings'].append(offering)
    deployment = deepcopy(resilient.state.document['deployments'][2])
    deployment.update(deployment_id='other-resource-deployment', offering_id='other-resource-model', wire_model_id='other-resource-wire')
    resilient.state.document['deployments'].append(deployment)
    if change == 'same_secret_reference':
        credential = deepcopy(resilient.state.document['credentials'][0])
        credential['credential_id'] = 'duplicated-source-credential'
        resilient.state.document['credentials'].append(credential)
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append((slot.routing_provider_id, slot.route_credential_id))
        if slot.oauth == 'codex':
            raise _error()
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is not None
    assert len(calls) == 4
    assert calls[-1][0] == 'provider-b'
    assert len({credential for provider, credential in calls if provider == 'provider-a'}) == 1



def test_sign_in_required_route_reselects_without_outage_evidence(resilient, monkeypatch, caplog):
    # Production shape: the cheapest summary route was a Codex login whose
    # refresh was rejected, so every compaction failed while main still worked.
    source = deepcopy(resilient.source)
    slots = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        slots.append(slot)
        if slot.oauth == 'codex':
            raise _sign_in_error()
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        actual = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    assert actual == '### Objective\nPreserve the authorized work.'
    # Retrying the same login cannot satisfy a sign-in requirement.
    assert [slot.routing_provider_id for slot in slots] == ['provider-a', 'provider-b']
    assert resilient.source == source and resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in slots)
    failed, succeeded = usage['_summaryAttempts']
    assert failed['status'] == 'failed' and failed['providerReached'] is False
    assert 'authIssue' not in failed and 'credentialSource' not in failed
    assert succeeded['status'] == 'succeeded'
    assert '_summaryFailure' not in usage
    assert 'PRIVATE-PREPARATION-PAYLOAD' not in caplog.text


@pytest.mark.parametrize('case', ['api_main', 'shared_resolver'])
def test_sign_in_required_uses_main_once_or_fails_without_typed_cause(resilient, monkeypatch, case):
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

    resilient.state.document['offerings'][1]['enabled'] = False
    if case == 'api_main':
        resilient.parent.oauth = ''
        # A paid main that fits never borrows the owner's login; this one
        # cannot fit the summary output, so the login is tried first.
        resilient.parent.max_output_tokens = 1024
    original = deepcopy(resilient.source)
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append((slot, kwargs.get('prefer_model')))
        if slot.oauth == 'codex':
            raise _sign_in_error()
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        result = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
    assert resilient.source == original and resilient.pool.slots == [resilient.parent]
    main_calls = [(slot, model) for slot, model in calls if slot is resilient.parent]
    assert len([slot for slot, _ in calls if slot.oauth == 'codex']) == 1
    if case == 'api_main':
        assert result == '### Objective\nPreserve the authorized work.'
        assert main_calls == [(resilient.parent, 'gpt-6.1-sol')]
        assert usage['_summaryRoute']['reason'] == 'main_after_auxiliary_credential_outage'
        assert [row['status'] for row in usage['_summaryAttempts']] == ['failed', 'succeeded']
        assert '_summaryFailure' not in usage
        return
    assert result is None and main_calls == []
    receipt = build_compaction_receipt(
        trigger='dispatch_guard', status='failed', strategy='unchanged',
        implementation='summary_dispatch', summary_usage=usage)
    # Sign-in expiry is not availability evidence (compaction_receipt_v1).
    assert '_summaryFailure' not in usage and 'failure' not in receipt['summary']
    [attempt] = receipt['summary']['attempts']
    assert attempt['status'] == 'failed' and attempt['providerReached'] is False
    assert 'authIssue' not in attempt


@pytest.mark.parametrize('observed', ['partial', 'reasoning', 'prior_ingress', 'cancel'])
def test_sign_in_required_after_observed_work_or_stop_never_reselects(resilient, monkeypatch, observed):
    original = deepcopy(resilient.source)
    slots = []

    def stream(*args, **kwargs):
        slots.append(_active(resilient))
        if observed == 'partial':
            kwargs['on_content']('incomplete text')
        elif observed == 'reasoning':
            kwargs['on_thinking']('private reasoning')
        elif observed == 'prior_ingress':
            kwargs['on_provider_attempt'](kind='complete', status='failed',
                                          errorKind='ProviderReportedNetworkError')
        else:
            resilient.task['aborted'] = True
        raise _sign_in_error()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is None
    assert len(slots) == 1
    assert resilient.source == original and resilient.pool.slots == [resilient.parent]

@pytest.mark.parametrize('failure', ['payload', 'partial', 'reasoning', 'prior_ingress', 'permanent', 'cancel', 'incomplete'])
def test_unsafe_or_owner_stopped_failure_never_reselects(resilient, monkeypatch, failure):
    original = deepcopy(resilient.source)
    slots = []
    observed = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        slots.append(slot)
        if failure == 'payload':
            raise LocalRequestPreparationError('PRIVATE-BAD-SCHEMA')
        if failure == 'partial':
            kwargs['on_content']('incomplete text')
            raise _error()
        if failure == 'reasoning':
            kwargs['on_thinking']('private reasoning')
            observed.append('reasoning')
            raise _error()
        if failure == 'prior_ingress':
            kwargs['on_provider_attempt'](kind='complete', status='failed',
                                          errorKind='ProviderReportedNetworkError')
            observed.append('prior_ingress')
            raise _error()
        if failure == 'permanent':
            raise _error('account_changed', transient=False)
        if failure == 'cancel':
            resilient.task['aborted'] = True
            raise _error()
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'unfinished'}, 'length', {'prompt_tokens': 20, 'completion_tokens': 10})

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is None
        assert get_pinned_provider() == _PARENT_PIN
    assert len(slots) == 1
    if failure in {'reasoning', 'prior_ingress'}:
        assert observed == [failure]
    assert resilient.source == original and resilient.pool.slots == [resilient.parent]
    assert slots[0].api_key == ''


@pytest.mark.parametrize('blocker', ['quote', 'cost', 'capacity', 'revoked', 'disabled'])
def test_reselection_cannot_cross_quote_cost_capacity_or_authority(resilient, monkeypatch, blocker):
    from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS, _SUMMARY_DISPATCH_MAX_TOKENS
    from lib.tasks_pkg.compaction._summary_routing import select_summary_route
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    if blocker == 'cost':
        # An invalid paid rate card is unknown, never free or a model-price
        # guess: it cannot qualify before or after the outage.
        resilient.state.document['credentials'][1]['kind'] = 'api_key'
        resilient.state.document['offerings'][1]['actual_pricing'].update(input=-1)
    elif blocker == 'capacity':
        resilient.state.document['offerings'][1]['context_window'] = 1
    elif blocker == 'disabled':
        resilient.state.document['offerings'][1]['enabled'] = False
    calls = []

    def stream(*args, **kwargs):
        calls.append(_active(resilient))
        if blocker == 'revoked':
            resilient.state.document['credentials'][1]['enabled'] = False
            resilient.state.revision += 1
        raise _error()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        tokens = _estimate_total_tokens([
            {'role': 'system', 'content': resilient.summary._SUMMARY_SYSTEM_PROMPT},
            {'role': 'user', 'content': 'fixed input'}])
        quote = select_summary_route(resilient.task, tokens, _SUMMARY_MAX_TOKENS,
                                     capacity_output_tokens=_SUMMARY_DISPATCH_MAX_TOKENS)
        assert quote.get('requestOwned')
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage,
            **({'expected_route': quote} if blocker == 'quote' else {})) is None
        assert get_pinned_provider() == _PARENT_PIN
    assert len(calls) == 3
    assert resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in calls)
    assert usage['_summaryFailure']['code'] == 'summary_credentials_unavailable'
    assert usage['_summaryFailure']['authIssue'] == 'codex_command_unavailable'


def test_no_alternative_failure_retains_safe_cause_and_deduplicates(resilient, monkeypatch, caplog):
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt

    resilient.state.document['offerings'][1]['enabled'] = False
    original = deepcopy(resilient.source)
    calls = []

    def stream(*args, **kwargs):
        calls.append(_active(resilient))
        raise _error()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage) is None
        receipt = build_compaction_receipt(
            trigger='dispatch_guard', status='failed', strategy='unchanged',
            implementation='summary_dispatch', summary_usage=usage)
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out={}) is None
    assert len(calls) == 3
    assert resilient.source == original
    assert receipt['summary']['failure']['code'] == 'summary_credentials_unavailable'
    assert receipt['summary']['failure']['authIssue'] == 'codex_command_unavailable'
    assert receipt['summary']['failure']['providerReached'] is False
    assert len(receipt['summary']['attempts']) == 1
    assert 'PRIVATE-PREPARATION-PAYLOAD' not in caplog.text


@pytest.mark.parametrize('case', ['token_api_main', 'task_pin_only', 'shared_resolver',
                                  'drift', 'quoted', 'main_fails'])
def test_auxiliary_outage_falls_back_once_to_authorized_main_route(resilient, monkeypatch, case):
    from contextlib import nullcontext
    from lib.tasks_pkg.compaction._constants import _SUMMARY_MAX_TOKENS, _SUMMARY_DISPATCH_MAX_TOKENS
    from lib.tasks_pkg.compaction._summary_routing import select_summary_route
    from lib.tasks_pkg.compaction._tokens import _estimate_total_tokens

    # Only the Codex resource auxiliary qualifies; no independent summary route exists.
    resilient.state.document['offerings'][1]['enabled'] = False
    if case != 'shared_resolver':
        resilient.parent.oauth = ''  # The task's own route is an ordinary API grant.
        # A paid main that fits never borrows the owner's login; this one
        # cannot fit the summary output, so the login is tried first.
        resilient.parent.max_output_tokens = 1024
    original_source = deepcopy(resilient.source)
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append((slot, kwargs.get('prefer_model')))
        if slot.oauth == 'codex':
            if case == 'drift':
                resilient.task['config']['disableModelFallback'] = True
            raise _error()
        if case == 'main_fails':
            raise RuntimeError('synthetic main provider failure')
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    # Production compaction may run without a thread pin; the task route group
    # alone must still identify the authorized main route.
    with (nullcontext() if case == 'task_pin_only' else provider_pin(_PARENT_PIN)):
        outer_pin = get_pinned_provider()
        options = {}
        if case == 'quoted':
            tokens = _estimate_total_tokens([
                {'role': 'system', 'content': resilient.summary._SUMMARY_SYSTEM_PROMPT},
                {'role': 'user', 'content': 'fixed input'}])
            options['expected_route'] = select_summary_route(
                resilient.task, tokens, _SUMMARY_MAX_TOKENS,
                capacity_output_tokens=_SUMMARY_DISPATCH_MAX_TOKENS)
            assert options['expected_route'].get('requestOwned')
        result = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage, **options)
        assert get_pinned_provider() == outer_pin
    codex_calls = [slot for slot, _ in calls if slot.oauth == 'codex']
    main_calls = [(slot, model) for slot, model in calls if slot is resilient.parent]
    assert len(codex_calls) == 3 and all(slot.api_key == '' for slot in codex_calls)
    assert resilient.pool.slots == [resilient.parent]
    assert resilient.source == original_source
    if case in ('token_api_main', 'task_pin_only'):
        assert result == '### Objective\nPreserve the authorized work.'
        assert main_calls == [(resilient.parent, 'gpt-6.1-sol')]
        assert usage['_summaryRoute']['reason'] == 'main_after_auxiliary_credential_outage'
        assert [row['status'] for row in usage['_summaryAttempts']] == ['failed', 'succeeded']
        assert '_summaryFailure' not in usage
        return
    assert result is None
    if case == 'main_fails':
        # Main reached its provider: the overall failure is not an unsent outage.
        assert main_calls == [(resilient.parent, 'gpt-6.1-sol')]
        assert '_summaryFailure' not in usage
    else:
        assert main_calls == []
        assert usage['_summaryFailure']['authIssue'] == 'codex_command_unavailable'


def test_main_fallback_reaches_independent_main_grant_beside_failed_codex_resolver(
        resilient, monkeypatch):
    # Production shape: the parent pin serves the main model through the failed
    # Codex login AND an API-key deployment. One shared-resolver slot must not
    # disqualify the grant the turn itself would dispatch to after the outage.
    import lib.llm as llm
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch import _api_stream
    from tests.compaction.test_summary_routing import _slot

    document = resilient.state.document
    document['offerings'][1]['enabled'] = False  # no independent auxiliary qualifies
    document['offerings'].append({
        **deepcopy(document['offerings'][0]), 'offering_id': 'sol-b',
        'provider_access_id': 'access-b', 'capabilities': ['text'],
        'actual_pricing': {'input': 1, 'output': 2, 'currency': 'USD',
                           'unit': 'per_million_tokens'}})
    document['deployments'].append({
        **deepcopy(document['deployments'][1]), 'deployment_id': 'sol-b-deployment',
        'offering_id': 'sol-b', 'wire_model_id': 'sol-wire-b'})
    resilient.parent = replace(
        resilient.parent, route_deployment_id='alpha-a-deployment',
        route_connection_id='connection-a', route_credential_id='credential-a')
    independent = replace(_slot(
        'gpt-6.1-sol', routing_provider_id='provider-b',
        route_offering_id='sol-b', route_deployment_id='sol-b-deployment',
        route_connection_id='connection-b', route_credential_id='credential-b'),
        key_name='summary-parent-api', api_key='offline-api', model='sol-wire-b')
    resilient.pool.slots = [resilient.parent, independent]

    monkeypatch.setattr(dispatch, 'dispatch_stream', _api_stream.dispatch_stream)
    monkeypatch.setattr(_api_stream, 'get_dispatcher', lambda: resilient.pool)
    monkeypatch.setattr('lib.llm_dispatch.cache_settle.settle_before_send', lambda *a, **k: None)
    monkeypatch.setattr(_api_stream, '_sleep_and_record_queue_wait', lambda *a, **k: None)
    monkeypatch.setattr(resilient.pool, 'reserve_shared_contention_probe',
                        lambda *a, **k: SimpleNamespace(delay_s=0.0, admitted=True))
    calls = []

    def transport(body, **kwargs):
        calls.append((body.get('model'), kwargs.get('oauth') or ''))
        if kwargs.get('oauth') == 'codex':
            raise _error('codex_home_unavailable')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nPreserve the authorized work.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10,
                     '_dispatch': {'model': 'gpt-6.1-sol', 'provider_id': 'provider-b'}})

    monkeypatch.setattr(llm, 'stream_chat', transport)
    usage = {}
    with provider_pin(_PARENT_PIN):
        actual = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    assert actual == '### Objective\nPreserve the authorized work.'
    assert calls[:3] == [('beta-wire-a', 'codex')] * 3
    assert calls[-1] == ('sol-wire-b', '')
    # Dispatch may probe the shared-resolver main slot once before excluding it.
    assert set(calls[3:-1]) <= {('wire-gpt-6.1-sol', 'codex')} and len(calls) <= 5
    assert usage['_summaryRoute']['reason'] == 'main_after_auxiliary_credential_outage'
    assert [row['status'] for row in usage['_summaryAttempts']] == ['failed', 'succeeded']
    assert '_summaryFailure' not in usage
    assert resilient.pool.slots == [resilient.parent, independent]
    assert resilient.parent.api_key == ''


@pytest.mark.parametrize('optout', ['main', 'config', 'task'])
def test_credential_outage_never_widens_explicit_policy(resilient, monkeypatch, optout):
    if optout == 'main':
        resilient.task['config']['compaction']['summaryPolicy'] = 'main'
    elif optout == 'config':
        resilient.task['config']['disableModelFallback'] = True
    else:
        resilient.task['disableModelFallback'] = True
    calls = []

    def stream(*args, **kwargs):
        calls.append(get_pinned_provider())
        raise _error()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is None
    assert calls == [_PARENT_PIN] * 3
    assert not resilient.state.resolved
    assert resilient.pool.slots == [resilient.parent]


def test_distinct_credential_for_same_model_remains_eligible(resilient, monkeypatch):
    # A model name is not a credential identity. Prefer another grant under the
    # same offering when it has an independent source (an API-key envelope).
    resilient.state.document['offerings'][1]['enabled'] = False
    credential = deepcopy(resilient.state.document['credentials'][0])
    credential.update(credential_id='independent-api', kind='subscription',
                      secret_reference='independent-api-secret')
    resilient.state.document['credentials'].append(credential)
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        if slot.oauth == 'codex':
            raise _error()
        assert slot.logical_model == 'gpt-6-luna'
        assert slot.route_credential_id == 'independent-api'
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is not None
    assert len(calls) == 4
    assert all(slot.api_key == '' for slot in calls)
    assert resilient.pool.slots == [resilient.parent]


def test_different_secret_reference_using_same_oauth_resolver_is_skipped(resilient, monkeypatch):
    import json
    import lib.model_routing as routing

    credential = deepcopy(resilient.state.document['credentials'][0])
    credential.update(credential_id='same-resolver-alias', secret_reference='alias-envelope')
    resilient.state.document['credentials'].append(credential)
    real_resolver = routing.ModelRoutingRepository.resolve_secret

    def resolve(self, owner, ref):
        if ref == 'alias-envelope':
            return json.dumps({'format': 'tofu.credential-secret/v1',
                               'api_key': 'offline-alias', 'oauth': 'codex'})
        return real_resolver(self, owner, ref)

    monkeypatch.setattr(routing.ModelRoutingRepository, 'resolve_secret', resolve)
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        if slot.oauth == 'codex':
            raise _error()
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is not None
    assert len(calls) == 4
    assert calls[-1].routing_provider_id == 'provider-b'
    assert resilient.pool.slots == [resilient.parent]


def test_reselected_allowance_is_requoted_from_original_not_failed_capacity(resilient, monkeypatch):
    from lib.tasks_pkg.compaction._constants import _SUMMARY_DISPATCH_MAX_TOKENS

    # Use the actual route key vocabulary, not a model-name special case: both
    # candidates share a logical model while one deployment has a smaller cap.
    resilient.state.document['deployments'][2]['max_output_tokens'] = 4096
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append((slot.routing_provider_id, kwargs['max_tokens']))
        if slot.oauth == 'codex':
            assert kwargs['max_tokens'] == 4096
            raise _error()
        assert kwargs['max_tokens'] == _SUMMARY_DISPATCH_MAX_TOKENS
        return _complete(slot)

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is not None
    assert len(calls) == 4


def test_cleanup_fault_during_outage_never_reselects_or_adopts(resilient, monkeypatch):
    from lib.model_routing import dispatch_adapter

    dispose = dispatch_adapter.dispose_routed_slot_group
    calls = []

    def close_then_fail(group):
        dispose(group)
        raise RuntimeError('cleanup must stop reselection')

    def stream(*args, **kwargs):
        calls.append(_active(resilient))
        raise _error()

    monkeypatch.setattr(dispatch_adapter, 'dispose_routed_slot_group', close_then_fail)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN), pytest.raises(RuntimeError, match='cleanup must stop'):
        resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
    assert '_summaryFailure' not in usage
    assert resilient.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_dispatch_failed'
    assert len(calls) == 3
    assert resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in calls)


def test_route_reselection_is_bounded_and_request_lifecycle_not_reset(resilient, monkeypatch):
    # Independently qualified failed credentials cannot create an unbounded
    # loop, even if the owner catalog has more candidates than the retry bound.
    for index in range(5):
        credential = deepcopy(resilient.state.document['credentials'][1])
        credential.update(credential_id=f'api-{index}', secret_reference=f'api-source-{index}')
        resilient.state.document['credentials'].append(credential)
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append((slot.route_credential_id, slot.provider_id))
        raise _error()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage) is None
        request_key = resilient.task['_summaryAttemptBudget']['requestKey']
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out={}) is None
        assert resilient.task['_summaryAttemptBudget']['requestKey'] == request_key
    assert len(calls) == 9  # three same-source attempts per distinct route, three routes
    assert len(usage['_summaryAttempts']) == 3
    assert resilient.pool.slots == [resilient.parent]


@pytest.mark.parametrize('failure', ['unavailable', 'prior_ingress', 'reasoning'])
def test_real_dispatch_transport_provenance_prevents_false_unsent_reselection(resilient, monkeypatch, failure):
    import lib.llm as llm
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch import _api_stream

    resilient.pool.slots = [resilient.parent]

    # Keep the real picker, body adapter, attempt observer and summary pipeline.
    # Only the expensive provider transport and unrelated host waits are stubbed.
    monkeypatch.setattr(dispatch, 'dispatch_stream', _api_stream.dispatch_stream)
    monkeypatch.setattr(_api_stream, 'get_dispatcher', lambda: resilient.pool)
    monkeypatch.setattr('lib.llm_dispatch.cache_settle.settle_before_send', lambda *a, **k: None)
    monkeypatch.setattr(_api_stream, '_sleep_and_record_queue_wait', lambda *a, **k: None)
    monkeypatch.setattr(resilient.pool, 'reserve_shared_contention_probe',
                        lambda *a, **k: SimpleNamespace(delay_s=0.0, admitted=True))
    calls = []

    def transport(body, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        if slot.oauth != 'codex':
            return _complete(slot)
        if failure == 'prior_ingress' and len(calls) == 1:
            return ProviderStreamResult.from_legacy(
                {'role': 'assistant', 'content': ''}, 'network_error', {})
        if failure == 'reasoning':
            kwargs['on_thinking']('private streamed reasoning')
        raise _error()

    monkeypatch.setattr(llm, 'stream_chat', transport)
    usage = {}
    with provider_pin(_PARENT_PIN):
        actual = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    if failure == 'unavailable':
        assert actual is not None
        assert len(calls) == 4 and calls[-1].routing_provider_id == 'provider-b'
        assert usage['_summaryAttempts'][0]['providerReached'] is False
    else:
        assert actual is None
        assert len(calls) == (2 if failure == 'prior_ingress' else 1)
        assert '_summaryFailure' not in usage
        assert all(slot.routing_provider_id == 'provider-a' for slot in calls)
    assert resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in calls)


@pytest.mark.parametrize('drift', ['owner', 'tenant', 'policy', 'parent_pin', 'revision', 'capacity'])
def test_reselection_refuses_changed_original_authority_or_requote(resilient, monkeypatch, drift):
    calls = []

    def stream(*args, **kwargs):
        calls.append(_active(resilient))
        if drift == 'owner':
            resilient.task['_principalContext']['owner_user_id'] = 99
        elif drift == 'tenant':
            resilient.task['_principalContext']['tenant_id'] = 'other-tenant'
        elif drift == 'policy':
            resilient.task['config']['disableModelFallback'] = True
        elif drift == 'parent_pin':
            resilient.task['_pinned_provider_id'] = 'unissued-group'
        elif drift == 'revision':
            resilient.state.revision += 1
        else:
            resilient.state.document['offerings'][1]['context_window'] = 1
        raise _error()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin(_PARENT_PIN):
        assert resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task) is None
        assert get_pinned_provider() == _PARENT_PIN
    assert len(calls) == 3
    assert all(slot.api_key == '' for slot in calls)
    assert resilient.pool.slots == [resilient.parent]


@pytest.mark.parametrize(('authority_path', 'value'), [
    pytest.param(('config', 'disableModelFallback'), True, id='config_optout'),
    pytest.param(('disableModelFallback',), True, id='task_optout'),
    pytest.param(('config', 'compaction', 'summaryPolicy'), 'main', id='main_policy'),
    pytest.param(('_principalContext', 'owner_user_id'), 99, id='principal_owner'),
    pytest.param(('_principalContext', 'tenant_id'), 'other-tenant', id='principal_tenant'),
    pytest.param(('_pinned_provider_id',), 'unissued-group', id='task_pin'),
    pytest.param(('config', '_pinned_provider_id'), 'unissued-group', id='config_pin'),
])
def test_reselection_rechecks_authority_after_selection_before_mint(
        resilient, monkeypatch, authority_path, value):
    from lib.tasks_pkg.compaction import _auxiliary_route as auxiliary
    from lib.tasks_pkg.compaction import _summary_routing as routing

    real_select, real_mint = routing.select_summary_route, auxiliary.mint_auxiliary_route
    original_source = deepcopy(resilient.source)
    selections, minted, calls = [], [], []

    def select_then_mutate(*args, **kwargs):
        candidate = real_select(*args, **kwargs)
        selections.append(candidate)
        if len(selections) == 2:
            assert candidate.get('requestOwned') is True
            assert candidate['providerId'] == 'provider-b'
            assert candidate['policy'] == selections[0]['policy'] == 'aa-cheapest'
            assert candidate['routeIdentity']['revision'] == selections[0]['routeIdentity']['revision']
            assert candidate['_parentRouteIdentities'] == selections[0]['_parentRouteIdentities']
            assert resilient.pool.slots == [resilient.parent]
            authority = resilient.task
            for key in authority_path[:-1]:
                authority = authority[key]
            authority[authority_path[-1]] = value
        return candidate

    def mint(route, **kwargs):
        minted.append(route['providerId'])
        return real_mint(route, **kwargs)

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        if slot.oauth == 'codex':
            raise _error()
        return _complete(slot)

    monkeypatch.setattr(routing, 'select_summary_route', select_then_mutate)
    monkeypatch.setattr(auxiliary, 'mint_auxiliary_route', mint)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        result = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    assert len(selections) == 2
    assert minted == ['provider-a']
    assert [slot.routing_provider_id for slot in calls] == ['provider-a'] * 3
    assert result is None
    assert resilient.state.resolved == ['fixture-oauth']
    assert resilient.source == original_source and resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in calls)
    assert usage['_summaryRoute']['providerId'] == 'provider-a'
    assert [attempt['status'] for attempt in usage['_summaryAttempts']] == ['failed']
    assert '_summaryCalls' not in usage


@pytest.mark.parametrize(('authority_path', 'value'), [
    pytest.param(('config', 'disableModelFallback'), True, id='config_optout'),
    pytest.param(('disableModelFallback',), True, id='task_optout'),
    pytest.param(('config', 'compaction', 'summaryPolicy'), 'main', id='main_policy'),
    pytest.param(('_principalContext', 'owner_user_id'), 99, id='principal_owner'),
    pytest.param(('_principalContext', 'tenant_id'), 'other-tenant', id='principal_tenant'),
    pytest.param(('_pinned_provider_id',), 'unissued-group', id='task_pin'),
    pytest.param(('config', '_pinned_provider_id'), 'unissued-group', id='config_pin'),
])
def test_initial_selection_rechecks_authority_before_mint_or_dispatch(
        resilient, monkeypatch, authority_path, value):
    from lib.tasks_pkg.compaction import _auxiliary_route as auxiliary
    from lib.tasks_pkg.compaction import _summary_routing as routing

    real_select, real_mint = routing.select_summary_route, auxiliary.mint_auxiliary_route
    original_source = deepcopy(resilient.source)
    selections, minted, calls = [], [], []

    def select_then_mutate(*args, **kwargs):
        candidate = real_select(*args, **kwargs)
        selections.append(candidate)
        assert candidate.get('requestOwned') is True
        authority = resilient.task
        for key in authority_path[:-1]:
            authority = authority[key]
        authority[authority_path[-1]] = value
        return candidate

    def mint(route, **kwargs):
        minted.append(route['providerId'])
        return real_mint(route, **kwargs)

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        return _complete(slot)

    monkeypatch.setattr(routing, 'select_summary_route', select_then_mutate)
    monkeypatch.setattr(auxiliary, 'mint_auxiliary_route', mint)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        result = resilient.summary._generate_query_aware_summary(
            resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    assert result is None
    assert len(selections) == 1
    assert minted == [] and calls == []
    assert resilient.state.resolved == []
    assert resilient.source == original_source and resilient.pool.slots == [resilient.parent]
    assert '_summaryCalls' not in usage and '_summaryFailure' not in usage


@pytest.mark.parametrize('structured_principal', [False, True], ids=['compatibility', 'structured'])
def test_principal_compatibility_stamp_is_not_authority_drift(monkeypatch, structured_principal):
    import lib.llm_dispatch as dispatch
    from lib.identity import PrincipalContext
    from lib.tasks_pkg.compaction._layer2 import _summary as summary

    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    calls = []

    def chat(*args, **kwargs):
        calls.append(kwargs)
        return '### Objective\nPreserve the recovery state.', {
            'prompt_tokens': 100, 'completion_tokens': 30, 'finish_reason': 'stop'}

    monkeypatch.setattr(dispatch, 'dispatch_chat', chat)
    task = {'_userId': 1, 'config': {'model': 'test-model'}}
    if structured_principal:
        task['_principalContext'] = PrincipalContext.user(
            subject_id='user:1', owner_user_id=1).to_payload()
    result = summary._generate_query_aware_summary(
        [{'role': 'user', 'content': 'Preserve the recovery state.'}], 'continue', task=task)
    # The owner read stamps the identical principal onto older tasks. That
    # identity-preserving conversion must not look like a policy change.
    assert result == '### Objective\nPreserve the recovery state.'
    assert len(calls) == 1
    assert task['_principalContext']['owner_user_id'] == 1


@pytest.mark.parametrize('outcome', ['available_alternative', 'no_alternative', 'cleanup_fault'])
def test_real_summary_producer_compactor_and_root_admission_preserve_failure_identity(
        resilient, monkeypatch, outcome):
    from unittest.mock import MagicMock
    import lib.llm_dispatch as dispatch
    from lib.error_envelope import from_exception
    from lib.llm_dispatch import _api_stream
    from lib.model_routing import dispatch_adapter
    from lib.tasks_pkg.compaction import _prompt_admission as admission
    from lib.tasks_pkg.compaction._layer2 import _compact as compact
    from lib.tasks_pkg.compaction._handoff import is_compaction_handoff
    from lib.tasks_pkg.orchestrator import _root_agent_loop as root_loop

    resilient.pool.slots = [resilient.parent]
    if outcome != 'available_alternative':
        resilient.state.document['offerings'][1]['enabled'] = False
    source = [
        {'role': 'system', 'content': 'Required system policy.'},
        {'role': 'user', 'content': 'Continue the authorized recovery work.'},
        {'role': 'assistant', 'content': 'cold finding ' * 12000},
        {'role': 'user', 'content': 'Continue without deleting source history.'},
    ]
    original = deepcopy(source)
    events, provider_calls = [], []

    def transport(body, **kwargs):
        slot = _active(resilient)
        provider_calls.append(slot)
        if slot.oauth == 'codex':
            raise _error('codex_home_unavailable')
        return _complete(slot)

    def measure(messages, tools, task, **kwargs):
        total = 1000 + sum(len(str(message.get('content') or '')) for message in messages)
        return {'totalTokens': total, 'messageTokens': total, 'toolSchemaTokens': 0,
                'messageCount': len(messages), 'method': 'synthetic_complete'}

    def ceiling(task, **kwargs):
        task['_contextBudgetDecision'] = {
            'contextWindowTokens': 128000, 'windowSafetyTokens': 100000,
            'resolvedOutputTokens': 8192}
        return 100000, 100000

    monkeypatch.setattr(dispatch, 'dispatch_stream', _api_stream.dispatch_stream)
    monkeypatch.setattr(_api_stream, 'get_dispatcher', lambda: resilient.pool)
    monkeypatch.setattr(_api_stream, '_sleep_and_record_queue_wait', lambda *a, **k: None)
    monkeypatch.setattr('lib.llm_dispatch.cache_settle.settle_before_send', lambda *a, **k: None)
    monkeypatch.setattr(resilient.pool, 'reserve_shared_contention_probe',
                        lambda *a, **k: SimpleNamespace(delay_s=0.0, admitted=True))
    monkeypatch.setattr('lib.llm.stream_chat', transport)
    monkeypatch.setattr(compact, '_generate_query_aware_summary',
                        resilient.summary._generate_query_aware_summary)
    monkeypatch.setattr(compact, '_archive_transcript', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.manager.append_event',
                        lambda task, event: events.append(event))
    monkeypatch.setattr('lib.goal_runs.run_pins.update_objective_from_receipt', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.cache_tracking._roi.record_l2_compaction', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.try_pressure_l1', lambda *a, **k: False)
    monkeypatch.setattr('lib.tasks_pkg.compaction._pressure.retain_oversized_tool_batch', lambda *a, **k: False)
    monkeypatch.setattr(admission, 'refresh_turn_requirements', lambda *a, **k: None)
    monkeypatch.setattr(admission, 'restore_checklist_context', lambda *a, **k: None)
    monkeypatch.setattr(admission, 'recompose_context_after_compaction', lambda *a, **k: None)
    monkeypatch.setattr(admission, '_measure', measure)
    monkeypatch.setattr(admission, '_resolved_ceiling', ceiling)
    monkeypatch.setattr(root_loop, 'prepare_round_context', lambda *a, **k: None)
    monkeypatch.setattr('lib.tasks_pkg.context_composer.execution_allowance.notice_decision',
                        lambda *a, **k: None)
    body_builder, fallback = MagicMock(), MagicMock()
    monkeypatch.setattr(root_loop, 'build_round_request', body_builder)
    monkeypatch.setattr(root_loop, 'run_llm_call_with_fallback', fallback)
    if outcome == 'cleanup_fault':
        dispose = dispatch_adapter.dispose_routed_slot_group

        def close_then_fail(group):
            dispose(group)
            raise RuntimeError('synthetic route disposal failure')

        monkeypatch.setattr(dispatch_adapter, 'dispose_routed_slot_group', close_then_fail)

    with provider_pin(_PARENT_PIN):
        if outcome == 'available_alternative':
            measured = admission.enforce_dispatch_prompt_limit(
                source, [], resilient.task, round_num=0, model=resilient.task['model'])
            assert measured['totalTokens'] <= 100000
            assert any(is_compaction_handoff(message) for message in source)
            assert [slot.routing_provider_id for slot in provider_calls] == ['provider-a'] * 3 + ['provider-b']
        else:
            request = SimpleNamespace(task=resilient.task, messages=source,
                state=SimpleNamespace(model=resilient.task['model']), tid=resilient.task['id'],
                project_path='', project_enabled=False, cfg={})
            hooks = root_loop._RootLoopHooks(request)
            expected = LocalRequestPreparationError if outcome == 'no_alternative' else admission.ContextCompactionError
            with pytest.raises(expected) as raised:
                hooks._dispatch_open_round(0, [])
            envelope = from_exception(raised.value)
            assert source == original
            assert len(provider_calls) == 3
            assert len([event for event in events if event.get('type') == 'compaction_done']) == 1
            receipt = next(event['receipt'] for event in events if event.get('type') == 'compaction_done')
            assert receipt['status'] == 'failed' and receipt['summary']['accepted'] is False
            if outcome == 'no_alternative':
                assert envelope['authIssue'] == 'codex_home_unavailable'
                assert envelope['providerReached'] is False and envelope['retryable'] is True
                assert receipt['summary']['failure']['authIssue'] == 'codex_home_unavailable'
            else:
                assert raised.value.__cause__ is None
                assert envelope['kind'] == 'internal' and 'authIssue' not in envelope
                assert 'failure' not in receipt['summary']
        assert get_pinned_provider() == _PARENT_PIN
    body_builder.assert_not_called()
    fallback.assert_not_called()
    assert resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in provider_calls)
    assert original[0] in source and original[-1] in source


@pytest.mark.parametrize('failure', ['admission_cancel', 'base_exception', 'observed_base_exception'])
def test_later_route_cancellation_or_fatal_exit_withdraws_previous_credential_cause(
        resilient, monkeypatch, failure):
    from lib.llm.llm_errors import AbortedError
    from lib.tasks_pkg.compaction._summary_failure import reconstructed_summary_failure
    calls = []

    def stream(*args, **kwargs):
        slot = _active(resilient)
        calls.append(slot)
        if slot.oauth == 'codex':
            raise _error()
        if failure == 'admission_cancel':
            raise AbortedError('canonical execution account refused')
        if failure == 'observed_base_exception':
            kwargs['on_provider_attempt'](kind='complete', status='failed', errorKind='KeyboardInterrupt')
        raise KeyboardInterrupt()

    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    usage = {}
    with provider_pin(_PARENT_PIN):
        if failure == 'admission_cancel':
            assert resilient.summary._generate_query_aware_summary(
                resilient.source, 'continue', task=resilient.task, usage_out=usage) is None
        else:
            with pytest.raises(KeyboardInterrupt):
                resilient.summary._generate_query_aware_summary(
                    resilient.source, 'continue', task=resilient.task, usage_out=usage)
        assert get_pinned_provider() == _PARENT_PIN
    assert len(calls) == 4
    assert '_summaryFailure' not in usage
    assert reconstructed_summary_failure(resilient.task, summary_usage=usage) is None
    assert resilient.pool.slots == [resilient.parent]
    assert all(slot.api_key == '' for slot in calls)

"""Whole issued-parent readiness and quote fences, with genuine offline mints."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
import socket

import pytest

from lib.identity import PrincipalContext
from lib.llm_dispatch import ephemeral, factory
from lib.llm_dispatch.dispatcher import LLMDispatcher
from lib.llm_dispatch.native_context_scope import (
    native_compaction_route_ready, native_context_scope,
)
from lib.llm_dispatch.provider_pin import get_pinned_provider, provider_pin
from lib.llm.stream_result import ProviderStreamResult
from lib.model_routing import ModelRef, NativeModelSelection, OwnerBoundary
from lib.model_routing import dispatch_adapter as adapter
from lib.model_routing.domain import ProviderOfferingRef
from lib.model_routing.inference import candidate_inference_metadata
from lib.model_routing.repository import StoredAuthority
from lib.tasks_pkg.compaction import _summary_routing as routing
from lib.tasks_pkg.compaction._layer2 import _summary as summary
from tests.model_routing.test_model_routing_contract import _full_document
from tests.support.model_registration import complete_registration

pytestmark = pytest.mark.unit


@pytest.fixture
def issued(monkeypatch, request):
    boundary = OwnerBoundary.create(23, getattr(request, 'param', 'tenant-a'))
    doc = _full_document()
    doc['offerings'][1]['model'] = {'creator_id': 'creator', 'model_id': 'beta'}
    second = deepcopy(doc['credentials'][0])
    second.update(credential_id='credential-a-second', secret_reference='second-key')
    second['authorization']['models'] = [{'creator_id': 'creator', 'model_id': 'alpha'}]
    doc['credentials'].append(second)
    for credential in doc['credentials']:
        credential.update(kind='api_key', secret_reference=credential['secret_reference']
                          or credential['credential_id'] + '-key')
    for deployment in doc['deployments']:
        deployment['max_output_tokens'] = 65536
    doc = complete_registration(doc)
    state = SimpleNamespace(document=doc, revision=7, resolved=[], reads=[])

    class Repository:
        def get(self, actual):
            assert actual == boundary
            state.reads.append(actual)
            document = deepcopy(state.document)
            document['revision'] = state.revision
            return StoredAuthority(boundary, state.revision, document, 0)

        def resolve_secret(self, actual, reference):
            assert actual == boundary
            state.resolved.append(reference)
            return 'offline-' + reference

    repository, pool = Repository(), LLMDispatcher()
    monkeypatch.setattr(factory, 'get_dispatcher', lambda: pool)
    monkeypatch.setattr('lib.llm_dispatch.get_dispatcher', lambda: pool)
    monkeypatch.setattr('lib.model_routing.ModelRoutingRepository', lambda: repository)
    monkeypatch.setattr(ephemeral, '_handles', {})
    monkeypatch.setattr(ephemeral, 'audit_log', lambda *a, **kw: None)
    monkeypatch.setattr(ephemeral, '_preflight_enabled', lambda: False)
    monkeypatch.setattr(socket, 'getaddrinfo', lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', port))])
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit', lambda p, m, w: w)
    monkeypatch.setattr('lib.model_catalog.aa.cached_routing_scores', lambda models: {})
    monkeypatch.setattr('lib.model_routing.health.existing_owner_health_registry', lambda *a: None)
    monkeypatch.setattr('lib.key_stats.key_admission_batch', lambda: nullcontext())
    monkeypatch.setattr('lib.key_stats.is_key_refused_for_optional_work', lambda *a: False)
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(summary, '_build_summary_user_content', lambda **kw: 'fixed offline source')
    groups = []

    def mint(selection=None):
        group = adapter.mint_routed_slot_group(repository, boundary, selection or
            NativeModelSelection(None, ProviderOfferingRef('provider-a', 'alpha-a')))
        groups.append((group, list(group.handles)))
        return group

    group = mint()
    assert isinstance(group, adapter.RoutedSlotGroup) and len(group.handles) == 2
    task = {'id': 'issued-parent-summary', 'convId': 'issued-parent-conversation',
            '_userId': 23, 'model': 'alpha',
            '_pinned_provider_id': group.pin_id, 'aborted': False,
            '_model_routing_group': group,
            '_principalContext': PrincipalContext.user(subject_id='issued-fixture',
                owner_user_id=23, tenant_id=boundary.tenant_id).to_payload(),
            'config': {'model': 'alpha', 'compaction': {'summaryPolicy': 'aa-cheapest'}}}
    state.resolved.clear()
    yield SimpleNamespace(task=task, group=group, pool=pool, state=state, mint=mint,
        boundary=boundary, source=[{'role': 'user', 'content': 'Keep the original evidence.'}])
    for group, handles in groups:
        group.handles, group.disposed = handles, False
        for handle in handles:
            handle.disposed = False
        adapter.dispose_routed_slot_group(group)
    assert ephemeral.count_ephemeral_slots() == 0 and not pool.slots


@contextmanager
def bound(issued, scoped):
    group = issued.task['_model_routing_group']
    candidate = group.candidates[0]
    witness = group.route_identity
    metadata = dict(candidate_inference_metadata(candidate),
        owner_user_id=23, tenant_id=issued.boundary.tenant_id, revision=witness['revision'],
        provider_id=candidate.provider_id,
        provider_access_id=candidate.provider_access['provider_access_id'],
        offering_id=candidate.offering['offering_id'],
        deployment_id=candidate.deployment['deployment_id'],
        connection_id=candidate.connection['connection_id'],
        credential_id=candidate.credential['credential_id'],
        logical_model=candidate.model['model_id'], wire_model=candidate.deployment['wire_model_id'])
    with provider_pin(group.pin_id), (native_context_scope(issued.task, metadata, 1024)
                                     if scoped else nullcontext()):
        yield


def quote(issued):
    tokens = summary._summary_request_input_tokens(issued.source, 'continue', task=issued.task,
        source_messages=None, anchor_text='')
    return routing.select_summary_route(issued.task, tokens, summary._SUMMARY_MAX_TOKENS,
        capacity_output_tokens=summary._SUMMARY_DISPATCH_MAX_TOKENS)


def retain_main(issued):
    for offering in issued.state.document['offerings'][1:]:
        offering['actual_pricing'].update(input=100, output=100)


def test_mint_attaches_immutable_secret_free_witness_to_every_slot(issued):
    for handle, candidate in zip(issued.group.handles, issued.group.candidates):
        witness = handle.slot.route_witness
        assert dict(witness) == adapter.issued_route_identity(candidate, issued.boundary, 7)
        with pytest.raises(TypeError):
            witness['revision'] = 8
        assert 'offline-' not in str(witness) and 'secret_reference' not in witness


@pytest.mark.parametrize('scoped', [False, True])
def test_healthy_two_credential_parent_can_plan_paid_auxiliary(issued, monkeypatch, scoped):
    calls, before = [], deepcopy(issued.source)
    def stream(*a, **kw):
        with issued.pool._lock:
            slots = list(issued.pool._selection_scope(get_pinned_provider()))
        assert len(slots) == 1 and slots[0].logical_model == 'beta'
        assert kw['strict_model'] and kw['prefer_model'] == 'beta'
        calls.append(slots[0])
        return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': '### Objective\nAux summary.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with bound(issued, scoped):
        assert native_compaction_route_ready(issued.task)
        plan = quote(issued)
        assert plan.get('requestOwned') and plan['modelId'] == 'beta', plan
        assert len(plan['_parentRouteIdentities']) == 2 and not issued.state.resolved
        assert summary._generate_query_aware_summary(issued.source, 'continue', task=issued.task,
            expected_route=plan) == '### Objective\nAux summary.'
        assert get_pinned_provider() == issued.group.pin_id
    assert len(calls) == len(issued.state.resolved) == 1
    assert calls[0].api_key == '' and issued.source == before
    assert list(issued.pool.slots) == [handle.slot for handle in issued.group.handles]
    assert not issued.group.disposed


@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('fault', ['detach', 'dispose', 'owner', 'tenant', 'group_witness',
    'slot_witness', 'candidate', 'model', 'missing_candidate', 'disposed_group'])
def test_invalid_issued_sibling_refuses_before_discovery_or_transport(issued, monkeypatch, scoped, fault):
    group, slot = issued.group, issued.group.handles[1].slot
    calls = []
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', lambda *a, **kw: calls.append(kw))
    before = deepcopy(issued.source)
    with bound(issued, scoped):
        if fault == 'detach':
            with issued.pool._lock:
                issued.pool.slots.withdraw([slot])
        elif fault == 'dispose':
            ephemeral.dispose_ephemeral_slot(group.handles[1])
        elif fault == 'owner':
            slot.routing_owner_user_id = 24
        elif fault == 'tenant':
            slot.routing_tenant_id = 'foreign'
        elif fault == 'group_witness':
            group.route_identity = dict(group.route_identity, tenant_id='foreign')
        elif fault == 'slot_witness':
            slot.route_witness = dict(group.route_identity, credentialId='forged')
        elif fault == 'candidate':
            group.candidates[1].credential['credential_id'] = 'forged'
        elif fault == 'model':
            slot.logical_model, slot.model = 'unrelated', 'unrelated-wire'
        elif fault == 'missing_candidate':
            group.candidates.pop()
        else:
            group.disposed = True
        assert not native_compaction_route_ready(issued.task)
        reads = len(issued.state.reads)
        plan = quote(issued)
        assert plan['reason'] == 'native_parent_route_unready' and not plan.get('requestOwned')
        assert len(issued.state.reads) == reads
        assert summary._generate_query_aware_summary(issued.source, 'continue', task=issued.task) is None
    assert not calls and not issued.state.resolved and issued.source == before


@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('detach', [False, True])
def test_whole_group_includes_nonprimary_model_members(issued, scoped, detach):
    group = issued.mint(NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a'))
    issued.task.update(_model_routing_group=group, _pinned_provider_id=group.pin_id)
    sibling = next(handle.slot for handle in group.handles if handle.slot.logical_model == 'beta')
    with bound(issued, scoped):
        assert native_compaction_route_ready(issued.task)
        if detach:
            with issued.pool._lock:
                issued.pool.slots.withdraw([sibling])
            assert not native_compaction_route_ready(issued.task)
        else:
            plan = quote(issued)
            assert len(plan['_parentRouteIdentities']) == len(group.handles)


@pytest.mark.parametrize('fault', ['revoked_secondary', 'revision'])
def test_original_parent_refusal_is_not_lost_on_paid_main(issued, fault):
    if fault == 'revision':
        issued.state.revision += 1
    else:
        issued.state.document['credentials'][2]['enabled'] = False
    assert native_compaction_route_ready(issued.task)  # Issued local membership is intact.
    plan = quote(issued)
    assert plan['reason'] == 'parent_route_changed' and not plan.get('requestOwned'), plan
    assert not issued.state.resolved


@pytest.mark.parametrize('drift', ['coherent_secondary', 'coherent_revision'])
def test_same_price_retained_main_quote_binds_detached_whole_group(issued, monkeypatch, drift):
    retain_main(issued)
    expected = quote(issued)
    assert not expected.get('requestOwned') and expected['modelId'] == 'alpha'
    original_quote = deepcopy(expected)
    original_authority = summary._summary_authority_evidence(issued.task)
    group = issued.group
    if drift == 'coherent_secondary':
        credential = deepcopy(group.candidates[1].credential)
        credential['credential_id'] = 'credential-a-rotated'
        issued.state.document['credentials'].append(deepcopy(credential))
        group.candidates[1] = replace(group.candidates[1], credential=credential)
        group.handles[1].slot.route_credential_id = credential['credential_id']
    else:
        issued.state.revision += 1
    for handle, candidate in zip(group.handles, group.candidates):
        handle.slot.route_witness = MappingProxyType(adapter.issued_route_identity(
            candidate, issued.boundary, issued.state.revision))
    group.route_identity = group.primary.slot.route_witness
    actual = quote(issued)
    assert native_compaction_route_ready(issued.task)
    assert actual['projectedCostUsd'] == expected['projectedCostUsd']
    assert expected == original_quote  # No candidate or slot dictionary aliases the quote.
    assert summary._summary_authority_evidence(issued.task) != original_authority
    assert not summary._summary_route_matches_quote(actual, expected)
    missing = dict(expected)
    missing.pop('_parentRouteIdentities')
    assert not summary._summary_route_matches_quote(missing, missing)
    calls, usage = [], {}
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', lambda *a, **kw: calls.append(kw))
    before = deepcopy(issued.source)
    assert summary._generate_query_aware_summary(issued.source, 'continue', task=issued.task,
        expected_route=expected, usage_out=usage) is None
    assert issued.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_route_revalidation_failed'
    assert not calls and not issued.state.resolved and issued.source == before
    refused_key = issued.task['_summaryAttemptBudget']['requestKey']
    fresh = quote(issued)
    def complete(*a, **kw):
        calls.append(kw)
        return ProviderStreamResult.from_legacy({'role': 'assistant', 'content': '### Objective\nFresh summary.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', complete)
    with provider_pin(group.pin_id):
        assert summary._generate_query_aware_summary(issued.source, 'continue', task=issued.task,
            expected_route=fresh) == '### Objective\nFresh summary.'
    assert issued.task['_summaryAttemptBudget']['requestKey'] != refused_key
    assert '_summaryBudgetFailure' not in issued.task['_summaryAttemptBudget']
    assert len(calls) == 1 and not issued.state.resolved and issued.source == before


@pytest.mark.parametrize('scoped', [False, True])
@pytest.mark.parametrize('optout', ['main', 'config_disable', 'task_disable'])
def test_optouts_keep_whole_parent_quote_without_auxiliary_compile_or_mint(issued, monkeypatch, scoped, optout):
    if optout == 'main':
        issued.task['config']['compaction']['summaryPolicy'] = 'main'
    elif optout == 'config_disable':
        issued.task['config']['disableModelFallback'] = True
    else:
        issued.task['disableModelFallback'] = True
    def forbidden(*a, **kw):
        pytest.fail('explicit opt-out performed auxiliary compile or mint')
    monkeypatch.setattr('lib.model_routing.routing.RouteCandidateCompiler.compile', forbidden)
    monkeypatch.setattr(adapter, 'mint_routed_slot_group', forbidden)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', lambda *a, **kw:
        ProviderStreamResult.from_legacy({'role': 'assistant', 'content': '### Objective\nMain summary.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10}))
    with bound(issued, scoped):
        plan = quote(issued)
        assert plan['policy'] == 'main' and not plan.get('requestOwned')
        assert len(plan['_parentRouteIdentities']) == 2
        assert plan['routeIdentity']['credentialId'] == issued.group.candidates[0].credential['credential_id']
        assert summary._generate_query_aware_summary(issued.source, 'continue', task=issued.task,
            expected_route=plan) == '### Objective\nMain summary.'
    assert not issued.state.resolved


def test_no_issued_group_retains_legacy_control(issued):
    issued.task.pop('_model_routing_group')
    assert native_compaction_route_ready(issued.task)
    assert quote(issued).get('requestOwned')


@pytest.mark.parametrize('policy', ['main', 'aa-cheapest'])
def test_discovery_wait_cannot_rebase_a_coherent_issued_sibling(issued, monkeypatch, policy):
    from lib.model_profiles import configured_model_profiles
    retain_main(issued)
    issued.task['config']['compaction']['summaryPolicy'] = policy
    calls, usage = [], {}
    before = deepcopy(issued.source)

    def profile_read(**kwargs):
        profiles = configured_model_profiles(**kwargs)
        credential = deepcopy(issued.group.candidates[1].credential)
        credential['credential_id'] = 'credential-a-after-wait'
        issued.state.document['credentials'].append(deepcopy(credential))
        issued.group.candidates[1] = replace(issued.group.candidates[1], credential=credential)
        slot = issued.group.handles[1].slot
        slot.route_credential_id = credential['credential_id']
        slot.route_witness = MappingProxyType(adapter.issued_route_identity(
            issued.group.candidates[1], issued.boundary, issued.state.revision))
        return profiles

    monkeypatch.setattr('lib.model_profiles.configured_model_profiles', profile_read)
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', lambda *a, **kw: calls.append(kw))
    with bound(issued, True):
        assert summary._generate_query_aware_summary(issued.source, 'continue', task=issued.task,
            usage_out=usage) is None
        assert native_compaction_route_ready(issued.task)  # Coherent new state is not the old approval.
    assert issued.task['_summaryAttemptBudget']['_summaryBudgetFailure'] == 'summary_route_revalidation_failed'
    assert not calls and not issued.state.resolved and issued.source == before


@pytest.mark.parametrize('scoped', [False, True])
def test_genuine_readiness_and_planner_only_read_the_indexed_scope(issued, monkeypatch, scoped):
    monkeypatch.setattr(type(issued.pool.slots), '__iter__', lambda self:
        pytest.fail('summary readiness/planning scanned unrelated live slots'))
    with bound(issued, scoped):
        assert native_compaction_route_ready(issued.task)
        assert quote(issued).get('requestOwned')


@pytest.mark.parametrize('issued', [None, ''], indirect=True)
def test_absent_and_empty_tenants_keep_genuine_issued_authority(issued):
    issued.task['_tenant_id'] = issued.task['_principalContext']['tenant_id']
    with bound(issued, True):
        assert native_compaction_route_ready(issued.task)
        assert quote(issued).get('requestOwned')

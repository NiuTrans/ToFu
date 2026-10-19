"""Owner compiler -> private mint -> L2 dispatch, entirely offline."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from lib.model_routing import OwnerBoundary, normalize_document
from lib.model_routing.repository import StoredAuthority
from lib.tasks_pkg.compaction._summary_routing import select_summary_route
from tests.model_routing.test_model_routing_contract import _full_document
from tests.compaction.test_summary_routing import _slot

pytestmark = pytest.mark.unit


@pytest.fixture
def native(monkeypatch):
    import lib.model_routing as routing
    import lib.model_catalog.aa as aa
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch.dispatcher import LLMDispatcher
    from lib.tasks_pkg.compaction._layer2 import _summary as summary
    doc = _full_document()
    names = {'alpha': 'gpt-6.1-sol', 'beta': 'gpt-6-luna'}
    for model in doc['models']:
        model['model_id'] = names[model['model_id']]
    for credential in doc['credentials']:
        for ref in credential['authorization']['models']:
            ref['model_id'] = names[ref['model_id']]
    for offering in doc['offerings']:
        offering['model']['model_id'] = names[offering['model']['model_id']]
    doc['offerings'][1]['model']['model_id'] = 'gpt-6-luna'
    doc['offerings'][0].pop('actual_pricing')
    doc['offerings'][2].pop('actual_pricing')
    doc['offerings'][2]['context_window'] = 150_000
    doc['credentials'][0].update(kind='oauth', secret_reference='fixture-oauth')
    doc['credentials'][1].update(kind='api_key', secret_reference='fixture-api')
    for deployment in doc['deployments']:
        deployment.pop('max_output_tokens', None)
    boundary = OwnerBoundary.create(41, 'tenant-a')
    state = SimpleNamespace(document=normalize_document(doc), revision=9, resolved=[])

    class Repo:
        def get(self, actual):
            assert actual == boundary
            return StoredAuthority(boundary, state.revision, deepcopy(state.document), 0)
        def resolve_secret(self, actual, ref):
            assert actual == boundary
            state.resolved.append(ref)
            return json.dumps({'format': 'tofu.credential-secret/v1',
                               'api_key': 'offline-fixture',
                               'oauth': 'codex' if ref == 'fixture-oauth' else ''})

    monkeypatch.setattr(routing, 'ModelRoutingRepository', Repo)
    monkeypatch.setattr(aa, 'cached_routing_scores', lambda models: {})
    pool = LLMDispatcher()
    # The only parent slot is Sol. No Luna/API slot exists before dispatch.
    parent = _slot('gpt-6.1-sol', route_offering_id='alpha-a',
                   routing_provider_id='provider-a', oauth='codex')
    pool.slots = [parent]
    monkeypatch.setattr(dispatch, 'get_dispatcher', lambda: pool)

    monkeypatch.setattr('lib.llm_dispatch.factory.get_dispatcher', lambda: pool)
    monkeypatch.setattr(pool, 'initialize', lambda: None)
    monkeypatch.setattr('lib.context_limits.resolve_learned_context_limit', lambda p, m, w: w)
    monkeypatch.setenv('TOFU_EPHEMERAL_PREFLIGHT', '0')

    monkeypatch.setenv('TOFU_BYO_ALLOW_HOSTS', 'a.example,b.example')
    monkeypatch.setattr(summary, '_codex_subscription_provider', lambda task: '')
    monkeypatch.setattr(summary, '_archived_prior_receipt', lambda task: '')
    monkeypatch.setattr(summary, '_summary_input_char_budget', lambda task: 12000)
    monkeypatch.setattr(summary, '_build_summary_user_content', lambda **kw: 'fixed input')
    task = {'_userId': 41, 'model': 'gpt-6.1-sol',
            '_pinned_provider_id': 'model-route:41:task-a',
            '_principalContext': {'kind': 'user', 'subject_id': 'user:41',
                                 'owner_user_id': 41, 'tenant_id': 'tenant-a', 'scopes': []},
            'config': {'compaction': {'summaryPolicy': 'aa-cheapest'}}}
    return SimpleNamespace(task=task, pool=pool, parent=parent, state=state,
                           summary=summary, source=[{'role': 'user', 'content': 'Keep source.'}])


@pytest.mark.parametrize('paid', [False, True])
def test_sol_subscription_dispatches_private_luna_or_authorized_paid_api(native, monkeypatch, paid):
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch.provider_pin import provider_pin, get_pinned_provider
    from lib.llm.stream_result import ProviderStreamResult
    if paid:
        native.state.document['offerings'][2]['enabled'] = False
        # Capacity quote (65536 output) must fit the paid ceiling.
        native.state.document['offerings'][1]['actual_pricing'].update(input=.1, output=.5)
    original_task, original_source = deepcopy(native.task), deepcopy(native.source)
    calls = []
    def stream(*args, **kwargs):
        pin = get_pinned_provider()
        assert pin != 'model-route:41:task-a'
        auxiliary = [slot for slot in native.pool.slots if slot.provider_id == pin]
        assert len(auxiliary) == 1
        slot = auxiliary[0]
        assert slot.logical_model == 'gpt-6-luna'
        assert slot.routing_provider_id == ('provider-b' if paid else 'provider-a')
        assert kwargs['strict_model'] and kwargs['max_429_attempts'] == 2
        calls.append(slot)
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nComplete summary.'}, 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 10,
             '_dispatch': {'model': slot.logical_model, 'provider_id': slot.routing_provider_id}})
    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    usage = {}
    with provider_pin('model-route:41:task-a'):
        plan = select_summary_route(native.task, 100, 2048, capacity_output_tokens=65536)
        assert plan.get('requestOwned') and plan['modelId'] == 'gpt-6-luna', plan
        assert not native.state.resolved
        result = native.summary._generate_query_aware_summary(
            native.source, 'continue', task=native.task, usage_out=usage,
            on_delta=lambda chunk: None)
        assert get_pinned_provider() == 'model-route:41:task-a'
    assert result == '### Objective\nComplete summary.'
    assert len(calls) == 1
    assert native.pool.slots == [native.parent]
    assert calls[0].api_key == ''  # disposal wipes request credentials
    assert native.task == original_task and native.source == original_source
    assert len(native.state.resolved) == 1
    if paid:
        assert usage['_summaryCalls'][0]['cost']['known'] is True
        assert usage['_summaryCalls'][0]['cost']['costUsd'] > 0
    else:
        assert plan['projectedCostUsd'] is None
        assert plan['costBasis']['marginalCostKnown'] is False
        assert plan['selectionBasis']['source'] == 'maintained_declared_rule'


@pytest.mark.parametrize('failure', ['error', 'incomplete', 'cancel', 'base_exception'])
def test_auxiliary_failure_never_widens_and_always_restores_and_disposes(native, monkeypatch, failure):
    import lib.llm_dispatch as dispatch
    from lib.llm_dispatch.provider_pin import provider_pin, get_pinned_provider
    from lib.llm.stream_result import ProviderStreamResult
    if failure == 'cancel':
        native.task['aborted'] = False
    calls = []
    def stream(*args, **kwargs):
        calls.append(get_pinned_provider())

        if failure == 'error':
            raise RuntimeError('offline failure')
        if failure == 'base_exception':
            raise KeyboardInterrupt()
        if failure == 'cancel':
            native.task['aborted'] = True
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': 'Partial'},
            'length' if failure == 'incomplete' else 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    with provider_pin('model-route:41:task-a'):
        if failure == 'base_exception':
            with pytest.raises(KeyboardInterrupt):
                native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task)
        else:
            assert native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task) is None
        assert get_pinned_provider() == 'model-route:41:task-a'
    assert len(calls) == 1
    assert native.pool.slots == [native.parent]


@pytest.mark.parametrize('optout', ['main', 'disable', 'cancel'])
def test_optouts_never_mint_credentials(native, monkeypatch, optout):
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    if optout == 'main':
        native.task['config']['compaction']['summaryPolicy'] = 'main'
    elif optout == 'disable':
        native.task['config']['disableModelFallback'] = True
    else:
        native.task['aborted'] = True
    monkeypatch.setattr(dispatch, 'dispatch_stream', lambda *a, **kw: ProviderStreamResult.from_legacy(
        {'role': 'assistant', 'content': '### Objective\nMain summary.'}, 'stop',
        {'prompt_tokens': 20, 'completion_tokens': 10}))
    native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task)
    assert not native.state.resolved
    assert native.pool.slots == [native.parent]


def test_route_revocation_at_mint_fails_before_secret_resolution(native):
    from contextlib import ExitStack
    from lib.tasks_pkg.compaction._auxiliary_route import mint_auxiliary_route
    from lib.model_routing import ModelRoutingError
    route = select_summary_route(native.task, 100, 2048, capacity_output_tokens=65536)
    assert route['requestOwned']
    native.state.revision += 1
    with ExitStack() as lifecycle, pytest.raises(ModelRoutingError):
        mint_auxiliary_route(route, owner=41, tenant='tenant-a', lifecycle=lifecycle)
    assert not native.state.resolved
    assert native.pool.slots == [native.parent]


def test_paid_main_uses_cross_provider_aa_cost_first_without_parent_mutation(native, monkeypatch):
    from tests.compaction.test_summary_routing import _aa
    monkeypatch.setattr('lib.model_catalog.aa.cached_routing_scores',
                        lambda models: {'creator::gpt-6-luna': _aa(50)})
    native.parent.oauth = ''
    native.parent.max_output_tokens = 128000
    native.state.document['offerings'][0]['actual_pricing'] = {
        'input': 10, 'output': 10, 'currency': 'USD', 'unit': 'per_million_tokens'}
    native.state.document['offerings'][2]['enabled'] = False
    native.state.document['offerings'][1]['actual_pricing'].update(input=.1, output=.5)
    from lib.model_profiles import configured_model_profiles
    profiles = configured_model_profiles(owner_user_id=41, tenant_id='tenant-a')
    assert any(profile.get('aaScores') for profile in profiles), [(profile['modelId'], profile.get('aaScores')) for profile in profiles]
    original = deepcopy(native.task)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan.get('requestOwned') and plan['providerId'] == 'provider-b', plan['reason']
    assert plan['modelId'] == 'gpt-6-luna'
    assert plan['projectedCostUsd'] < .03048
    assert plan['maximumCostUsd'] > plan['projectedCostUsd']
    assert native.task == original
    assert not native.state.resolved


@pytest.mark.parametrize('ceiling', [0, -1, True, 'invalid'])
def test_configured_ceiling_blocks_paid_subscription_alternate(native, ceiling):
    native.state.document['offerings'][2]['enabled'] = False
    native.task['config']['compaction']['summaryMaxCostUsd'] = ceiling
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['modelId'] == 'gpt-6.1-sol'
    assert not plan.get('requestOwned')
    assert not native.state.resolved


def test_complete_auxiliary_allowance_uses_shared_clamp_above_summary_target(native, monkeypatch):
    monkeypatch.setattr('lib.model_info._clamp_route_max_tokens', lambda model, tokens, **kw: 32768)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned']
    assert plan['capacityOutputTokens'] == 32768
    monkeypatch.setattr('lib.model_info._clamp_route_max_tokens', lambda model, tokens, **kw: 1024)
    refused = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert not refused.get('requestOwned')


def test_declared_selection_receipt_preserves_coherent_bounded_evidence(native):
    from lib.tasks_pkg.compaction._receipt import _summary_selection_details
    from jsonschema import Draft202012Validator
    from pathlib import Path
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    evidence = _summary_selection_details(plan['selectionBasis'])
    assert evidence['source'] == 'maintained_declared_rule'
    schema = json.loads((Path(__file__).parents[2] / 'contracts/compaction_receipt_v1.schema.json').read_text())
    validator = Draft202012Validator(schema['$defs']['summarySelection'])
    validator.validate(evidence)
    bad = {**evidence, 'source': 'Artificial Analysis'}
    assert list(validator.iter_errors(bad))
    assert _summary_selection_details(bad) == {}


def test_subscription_reference_proxy_is_explicit_never_marginal_cash(native, monkeypatch):
    monkeypatch.setattr('lib.pricing._tables.pricing_for_model',
                        lambda model: {'input': .1, 'output': .5})
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned']
    assert plan['projectedCostUsd'] is None
    assert plan['costBasis']['referenceProxyUsd'] == pytest.approx(.001124)
    assert plan['costBasis']['referenceSource'] == 'maintained_model_price_table'
    assert plan['costBasis']['marginalCostKnown'] is False


@pytest.mark.parametrize('primary_failure', [False, True])
def test_cleanup_failure_preserves_primary_and_cannot_adopt(native, monkeypatch, primary_failure):
    import lib.llm_dispatch as dispatch
    from lib.llm.stream_result import ProviderStreamResult
    from lib.llm_dispatch.provider_pin import provider_pin, get_pinned_provider
    from lib.model_routing import dispatch_adapter

    real_dispose = dispatch_adapter.dispose_routed_slot_group

    def dispose_then_fail(group):
        real_dispose(group)
        raise RuntimeError('offline cleanup fault')

    def stream(*args, **kwargs):
        if primary_failure:
            raise KeyboardInterrupt('primary stop')
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nComplete summary.'},
            'stop', {'prompt_tokens': 20, 'completion_tokens': 10})

    monkeypatch.setattr(dispatch_adapter, 'dispose_routed_slot_group', dispose_then_fail)
    monkeypatch.setattr(dispatch, 'dispatch_stream', stream)
    with provider_pin('model-route:41:task-a'):
        with pytest.raises(KeyboardInterrupt if primary_failure else RuntimeError):
            native.summary._generate_query_aware_summary(
                native.source, 'continue', task=native.task)
        assert get_pinned_provider() == 'model-route:41:task-a'
    assert native.pool.slots == [native.parent]


def test_default_summary_policy_selects_authorized_luna_without_parent_mutation(native):
    """Import/compiler API drift must not silently turn the default back into main."""
    from lib.llm_dispatch.provider_pin import provider_pin, get_pinned_provider

    native.task['config'].pop('compaction')
    original_task = deepcopy(native.task)
    with provider_pin('model-route:41:task-a'):
        plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
        assert plan['modelId'] == 'gpt-6-luna', plan
        assert plan['reason'] == 'authorized_auxiliary_resource_policy'
        assert plan['policy'] == 'aa-cheapest'
        assert plan['requestOwned'] is True
        assert get_pinned_provider() == 'model-route:41:task-a'
    assert native.task == original_task
    assert native.pool.slots == [native.parent]
    assert not native.state.resolved

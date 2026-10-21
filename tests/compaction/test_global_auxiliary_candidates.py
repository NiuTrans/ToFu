"""Compiler-backed, provider-independent compression candidate specifications."""
from copy import deepcopy

import pytest

from lib.tasks_pkg.compaction._summary_routing import select_summary_route
from tests.compaction.test_auxiliary_summary_integration import native as native  # noqa: F401

pytestmark = pytest.mark.unit


def _alternate(native, model, kind='subscription'):
    doc = native.state.document
    old = 'gpt-6-luna'
    for row in doc['models']:
        if row['model_id'] == old:
            row['model_id'] = model
    for row in doc['credentials']:
        for ref in row['authorization']['models']:
            if ref['model_id'] == old:
                ref['model_id'] = model
    for row in doc['offerings']:
        if row['model']['model_id'] == old:
            row['model']['model_id'] = model
    doc['offerings'][2]['enabled'] = False
    doc['credentials'][1]['kind'] = kind
    if kind != 'api_key':
        doc['offerings'][1].pop('actual_pricing', None)
    doc['providers'][1]['brand'] = 'yourprovider'
    return doc['offerings'][1]


@pytest.mark.parametrize('model', ['glm-5.3-flash', 'GEM5.3Flash', 'new-admitted-text-model'])
@pytest.mark.parametrize('main_paid', [False, True])
def test_all_admitted_families_participate_without_aa_or_name_allowlist(native, model, main_paid):
    offering = _alternate(native, model)
    if main_paid:
        native.parent.oauth = ''
        # Neither missing main pricing nor oversized parent output can block
        # an authorized subscription auxiliary with its own sufficient capacity.
        native.parent.max_output_tokens = 1024
    before = deepcopy(native.task)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'], plan
    assert plan['modelId'] == model
    assert plan['providerId'] == 'provider-b'
    assert plan['routeIdentity']['offeringId'] == offering['offering_id']
    assert plan['routeIdentity']['credentialId'] == native.state.document['credentials'][1]['credential_id']
    assert plan['projectedCostUsd'] is None
    assert plan['costBasis']['marginalCostKnown'] is False
    assert plan['selectionBasis']['eligibleRoutes'] >= 1
    assert plan['selectionBasis']['source'] != 'Artificial Analysis'
    if model != 'glm-5.3-flash':
        assert plan['selectionBasis']['source'] == 'routing_admission'
        assert plan['selectionBasis']['score'] is None
    assert native.task == before and not native.state.resolved
    assert native.pool.slots == [native.parent]


@pytest.mark.parametrize('model', ['glm-5.3-flash', 'new-admitted-text-model'])
def test_paid_alternate_needs_price_not_aa_coverage(native, model):
    offering = _alternate(native, model, 'api_key')
    native.parent.oauth = ''
    native.state.document['offerings'][0]['actual_pricing'] = {
        'input': 10, 'output': 10, 'currency': 'USD'}
    offering['actual_pricing'] = {'input': .1, 'output': .5, 'currency': 'USD'}
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'] and plan['modelId'] == model, plan
    assert plan['maximumCostUsd'] < .05
    assert plan['selectionBasis']['source'] != 'Artificial Analysis'
    offering.pop('actual_pricing')
    refused = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert not refused.get('requestOwned')


@pytest.mark.parametrize('failure', ['success', 'error', 'incomplete', 'cancel', 'base_exception'])
def test_api_key_backed_subscription_dispatch_has_one_private_route_and_tears_down(native, monkeypatch, failure):
    from lib.llm_dispatch.provider_pin import provider_pin, get_pinned_provider
    from lib.llm.stream_result import ProviderStreamResult
    _alternate(native, 'GEM5.3Flash')
    native.task['aborted'] = False
    seen = []
    def stream(*args, **kwargs):
        assert kwargs['prefer_model'] == 'GEM5.3Flash'
        assert kwargs['strict_model']
        pin = get_pinned_provider()
        assert pin != 'model-route:41:task-a'
        slots = [s for s in native.pool.slots if s.provider_id == pin]
        assert len(slots) == 1
        seen.append(slots[0])
        assert slots[0].api_key == 'offline-fixture' and not slots[0].oauth
        if failure == 'base_exception':
            raise KeyboardInterrupt()
        if failure == 'error':
            raise RuntimeError('offline test')
        if failure == 'cancel':
            native.task['aborted'] = True
        return ProviderStreamResult.from_legacy(
            {'role': 'assistant', 'content': '### Objective\nComplete cross-family summary.'},
            'length' if failure == 'incomplete' else 'stop',
            {'prompt_tokens': 20, 'completion_tokens': 10})
    monkeypatch.setattr('lib.llm_dispatch.dispatch_stream', stream)
    with provider_pin('model-route:41:task-a'):
        if failure == 'base_exception':
            with pytest.raises(KeyboardInterrupt):
                native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task)
        else:
            result = native.summary._generate_query_aware_summary(native.source, 'continue', task=native.task)
            assert bool(result) == (failure == 'success')
        assert get_pinned_provider() == 'model-route:41:task-a'
    assert len(seen) == 1
    assert seen[0].api_key == '' and seen[0].oauth == ''
    assert native.pool.slots == [native.parent]
    assert native.task['model'] == 'gpt-6.1-sol'


@pytest.mark.parametrize('invalid', ['disabled', 'revoked', 'capability', 'window', 'output'])
def test_unusable_or_unauthorized_alternate_is_refused(native, invalid):
    offering = _alternate(native, 'GEM5.3Flash')
    if invalid == 'disabled':
        offering['enabled'] = False
    elif invalid == 'revoked':
        native.state.document['credentials'][1]['enabled'] = False
    elif invalid == 'capability':
        offering['capabilities'] = ['embedding']
    elif invalid == 'window':
        offering['context_window'] = 2500
    else:
        native.state.document['deployments'][1]['max_output_tokens'] = 1024
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert not plan.get('requestOwned'), plan
    assert not native.state.resolved


def test_null_score_receipt_is_truthful_and_schema_valid(native):
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    from lib.tasks_pkg.compaction._receipt import _summary_selection_details
    _alternate(native, 'GEM5.3Flash')
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    evidence = _summary_selection_details(plan['selectionBasis'])
    assert evidence['score'] is None and evidence['source'] == 'routing_admission'
    schema = json.loads((Path(__file__).parents[2] / 'contracts/compaction_receipt_v1.schema.json').read_text())
    Draft202012Validator(schema['$defs']['summarySelection']).validate(evidence)
    assert _summary_selection_details({**evidence, 'score': 0}) == {}


@pytest.mark.parametrize('reverse', [False, True])
def test_first_paid_witness_cannot_hide_later_subscription_witness(native, reverse):
    from contextlib import ExitStack
    from lib.tasks_pkg.compaction._auxiliary_route import mint_auxiliary_route
    offering = _alternate(native, 'GEM5.3Flash', 'api_key')
    offering.pop('actual_pricing', None)
    credential = deepcopy(native.state.document['credentials'][1])
    credential.update(credential_id='z-subscription', kind='subscription')
    native.state.document['credentials'].append(credential)
    if reverse:
        native.state.document['credentials'].reverse()
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'], plan
    assert plan['routeIdentity']['credentialId'] == 'z-subscription'
    assert not native.state.resolved
    with ExitStack() as lifecycle:
        group = mint_auxiliary_route(plan, owner=41, tenant='tenant-a', lifecycle=lifecycle)
        slots = [slot for slot in native.pool.slots if slot.provider_id == group.pin_id]
        assert len(slots) == 1
        assert group.candidates[0].credential['credential_id'] == 'z-subscription'
        slot = slots[0]
    assert slot.api_key == '' and native.pool.slots == [native.parent]


@pytest.mark.parametrize('reverse', [False, True])
def test_low_output_first_deployment_cannot_hide_usable_later_deployment(native, reverse):
    _alternate(native, 'GEM5.3Flash')
    first = native.state.document['deployments'][1]
    first['max_output_tokens'] = 1024
    later = deepcopy(first)
    later.update(deployment_id='z-capable-deployment', max_output_tokens=32768,
                 wire_model_id='second-wire-b')
    native.state.document['deployments'].append(later)
    if reverse:
        native.state.document['deployments'].reverse()
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert plan['requestOwned'], plan
    assert plan['routeIdentity']['deploymentId'] == 'z-capable-deployment'
    assert 2048 <= plan['capacityOutputTokens'] <= 32768


@pytest.mark.parametrize('prices', [None, {'input': -1, 'output': .5},
                                  {'input': 0, 'output': 0}, {'input': .1, 'output': .5}])
def test_paid_price_unknown_invalid_and_explicit_zero_are_distinct(native, prices):
    offering = _alternate(native, 'GEM5.3Flash', 'api_key')
    if prices is None:
        offering.pop('actual_pricing', None)
    else:
        offering['actual_pricing'] = {**prices, 'currency': 'USD'}
    native.task['config']['compaction']['summaryMaxCostUsd'] = 0
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    free = prices is not None and prices['input'] == prices['output'] == 0
    assert bool(plan.get('requestOwned')) is free, plan
    if free:
        assert plan['maximumCostUsd'] == plan['projectedCostUsd'] == 0


def _bind_parent(slot, suffix):
    slot.route_deployment_id = f'alpha-{suffix}-deployment'
    slot.route_connection_id = f'connection-{suffix}'
    slot.route_credential_id = f'credential-{suffix}'


@pytest.mark.parametrize('parent_kind', ['subscription', 'api_key'])
def test_parent_billing_uses_full_native_credential_not_oauth_transport(native, parent_kind):
    _alternate(native, 'GEM5.3Flash', 'api_key')
    native.parent.oauth = ''
    _bind_parent(native.parent, 'a')
    native.state.document['credentials'][0].update(kind=parent_kind, secret_reference='fixture-api')

    native.parent.max_output_tokens = 128000
    native.state.document['offerings'][0].pop('actual_pricing', None)
    native.state.document['offerings'][1]['actual_pricing'] = {
        'input': .1, 'output': .5, 'currency': 'USD'}
    before = deepcopy(native.task)
    plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert bool(plan.get('requestOwned')) is (parent_kind == 'subscription'), plan
    if parent_kind == 'subscription':
        assert plan['modelId'] == 'GEM5.3Flash' and plan['maximumCostUsd'] < .05
    else:
        assert plan['reason'] == 'main_price_unknown'
    assert native.task == before and not native.state.resolved


@pytest.mark.parametrize('native_binding', [False, True])
def test_mixed_parent_can_plan_only_when_native_readiness_and_full_witnesses_hold(native, native_binding):
    from contextlib import nullcontext
    from lib.llm_dispatch.native_context_scope import native_context_scope
    from tests.compaction.test_summary_routing import _slot
    doc = native.state.document
    doc['offerings'][1]['model']['model_id'] = 'gpt-6.1-sol'
    doc['credentials'][1]['kind'] = 'subscription'
    _bind_parent(native.parent, 'a')
    second = _slot('gpt-6.1-sol', route_offering_id='alpha-b',
                   routing_provider_id='provider-b')
    second.model = 'alpha-wire-b'
    _bind_parent(second, 'b')
    native.pool.slots.append(second)
    before = deepcopy(native.task)
    if native_binding:
        native.task['id'] = 'task-a'
    metadata = {'owner_user_id': 41, 'provider_id': 'provider-a',
                'logical_model': 'gpt-6.1-sol', 'wire_model': native.parent.model,
                'context_window': 200000, 'offering_id': 'alpha-a',
                'deployment_id': 'alpha-a-deployment', 'connection_id': 'connection-a',
                'credential_id': 'credential-a'}
    # A single issued native binding cannot authorize a different provider
    # inside its parent group. The independent planner must not bypass it.
    scope = native_context_scope(native.task, metadata, 1024) if native_binding else nullcontext()
    with scope:
        plan = select_summary_route(native.task, 1000, 2048, capacity_output_tokens=65536)
    assert bool(plan.get('requestOwned')) is (not native_binding), plan
    if native_binding:
        assert plan['reason'] == 'native_parent_route_unready'
    else:
        assert plan['modelId'] == 'gpt-6-luna'
    assert not native.state.resolved
    assert native.pool.slots == [native.parent, second]
    assert native.task['model'] == before['model']

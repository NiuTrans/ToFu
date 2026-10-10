"""Executable boundary for the canonical model-routing v2 contract."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import copy

import pytest
from jsonschema import Draft202012Validator

from lib.model_routing import (
    CONTRACT_VERSION,
    MAX_COUNTS,
    MAX_ROUTE_SNAPSHOT_BYTES,
    HealthTarget,
    InMemoryModelRoutingRepository,
    ModelRef,
    ModelRoutingError,
    NativeModelSelection,
    OwnerBoundary,
    RouteHealthRegistry,
    RoutePolicy,
    RouteSnapshotBuilder,
    compile_candidates,
    compile_model_fallback_candidates,
    empty_document,
    execute_migration,
    normalize_document,
    parse_native_model_selection,
    resolve_compatible_model,
    public_projection,
    plan_legacy_migration,
)

from lib.provider_headers import is_credential_header, split_header_secrecy
from lib.secret_envelope import secret_hint


pytestmark = pytest.mark.unit


def _validator() -> Draft202012Validator:
    contract = (
        Path(__file__).parents[2]
        / 'contracts'
        / 'model_routing_v2.schema.json'
    )
    schema = json.loads(contract.read_text(encoding='utf-8'))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _full_document() -> dict:
    refs = [
        {'creator_id': 'creator', 'model_id': 'alpha'},
        {'creator_id': 'creator', 'model_id': 'beta'},
    ]
    return {
        'contract_version': CONTRACT_VERSION,
        'revision': 0,
        'creators': [{'creator_id': 'creator', 'name': 'Creator'}],
        'models': [{
            **refs[0], 'display_name': 'Alpha',
            'capabilities': ['text', 'thinking'], 'context_window': 200_000,
            'quality_rank': 100,
        }, {
            **refs[1], 'display_name': 'Beta',
            'capabilities': ['text'], 'context_window': 150_000,
            'quality_rank': 80,
        }],
        'providers': [{
            'provider_id': 'provider-a', 'name': 'Provider A', 'scope': 'public',
        }, {
            'provider_id': 'provider-b', 'name': 'Provider B', 'scope': 'owner',
        }],
        'provider_accesses': [{
            'provider_access_id': 'access-a', 'provider_id': 'provider-a',
            'enabled': True, 'quota_policy': {'rpm': 60},
        }, {
            'provider_access_id': 'access-b', 'provider_id': 'provider-b',
            'enabled': True, 'quota_policy': {},
        }],
        'connections': [{
            'connection_id': 'connection-a', 'provider_access_id': 'access-a',
            'base_url': 'https://a.example/v1', 'protocol': 'openai',
            'enabled': True, 'priority': 0, 'extra_headers': {},
        }, {
            'connection_id': 'connection-b', 'provider_access_id': 'access-b',
            'base_url': 'https://b.example/v1', 'protocol': 'openai',
            'enabled': True, 'priority': 0, 'extra_headers': {},
        }],
        'credentials': [{
            'credential_id': 'credential-a', 'provider_access_id': 'access-a',
            'kind': 'local_identity', 'secret_reference': '', 'key_hint': '',
            'enabled': True,
            'authorization': {
                'connection_ids': ['connection-a'], 'models': copy.deepcopy(refs),
            },
            'quota_policy': {},
        }, {
            'credential_id': 'credential-b', 'provider_access_id': 'access-b',
            'kind': 'local_identity', 'secret_reference': '', 'key_hint': '',
            'enabled': True,
            'authorization': {
                'connection_ids': ['connection-b'], 'models': copy.deepcopy(refs),
            },
            'quota_policy': {},
        }],
        'offerings': [{
            'offering_id': 'alpha-a', 'provider_access_id': 'access-a',
            'identity_state': 'confirmed', 'model': copy.deepcopy(refs[0]),
            'enabled': True, 'capabilities': ['text', 'thinking'],
            'context_window': 200_000, 'priority': 0,
            'actual_pricing': {
                'input': 5, 'output': 10, 'currency': 'USD',
                'unit': 'per_million_tokens',
            },
        }, {
            'offering_id': 'alpha-b', 'provider_access_id': 'access-b',
            'identity_state': 'confirmed', 'model': copy.deepcopy(refs[0]),
            'enabled': True, 'capabilities': ['text'],
            'context_window': 100_000, 'priority': 0,
            'actual_pricing': {
                'input': 2, 'output': 4, 'currency': 'USD',
                'unit': 'per_million_tokens',
            },
        }, {
            'offering_id': 'beta-a', 'provider_access_id': 'access-a',
            'identity_state': 'confirmed', 'model': copy.deepcopy(refs[1]),
            'enabled': True, 'capabilities': ['text'],
            'context_window': 150_000, 'priority': 0,
            'actual_pricing': {
                'input': 1, 'output': 2, 'currency': 'USD',
                'unit': 'per_million_tokens',
            },
        }],
        'deployments': [{
            'deployment_id': 'alpha-a-deployment', 'offering_id': 'alpha-a',
            'connection_id': 'connection-a', 'wire_model_id': 'alpha-wire-a',
            'enabled': True, 'identity_confidence': 'high',
            'probe_status': 'passed', 'priority': 0,
            'max_output_tokens': 65_535,
        }, {
            'deployment_id': 'alpha-b-deployment', 'offering_id': 'alpha-b',
            'connection_id': 'connection-b', 'wire_model_id': 'alpha-wire-b',
            'enabled': True, 'identity_confidence': 'high',
            'probe_status': 'passed', 'priority': 0,
        }, {
            'deployment_id': 'beta-a-deployment', 'offering_id': 'beta-a',
            'connection_id': 'connection-a', 'wire_model_id': 'beta-wire-a',
            'enabled': True, 'identity_confidence': 'high',
            'probe_status': 'passed', 'priority': 0,
        }],
    }


def test_domain_empty_document_is_valid_canonical_contract() -> None:
    document = normalize_document(empty_document())

    _validator().validate(document)
    assert document['contract_version'] == CONTRACT_VERSION
    assert document['revision'] == 0


def test_full_document_references_and_unique_wire_ids_validate() -> None:
    document = normalize_document(_full_document())
    _validator().validate(document)

    duplicate = copy.deepcopy(document)
    duplicate['deployments'][2]['wire_model_id'] = 'alpha-wire-a'
    with pytest.raises(ModelRoutingError) as error:
        normalize_document(duplicate)
    assert error.value.kind == 'duplicate_wire_model_id'


@pytest.mark.parametrize('model_id', ['grok-4.7', 'grok-4.20-multi-agent'])
def test_xai_publisher_prefix_is_not_model_identity(model_id: str) -> None:
    document = _full_document()
    document['creators'].append({'creator_id': 'xai', 'name': 'xAI'})
    prefixed = f'x-ai/{model_id}'
    document['models'].append({
        'creator_id': 'xai', 'model_id': prefixed, 'display_name': prefixed,
        'capabilities': ['text'], 'context_window': 500_000,
        'quality_rank': 1, 'registration_state': 'pending_metadata',
    })
    document['offerings'].append({
        'offering_id': 'grok-offering', 'provider_access_id': 'access-a',
        'identity_state': 'confirmed', 'model': {'creator_id': 'xai', 'model_id': prefixed},
        'enabled': True, 'capabilities': ['text'], 'context_window': 500_000,
        'priority': 0,
    })
    document['credentials'][0]['authorization']['models'].append(
        {'creator_id': 'xai', 'model_id': prefixed})
    document['deployments'].append({
        'deployment_id': 'grok-deployment', 'offering_id': 'grok-offering',
        'connection_id': 'connection-a', 'wire_model_id': prefixed,
        'identity_confidence': 'high', 'probe_status': 'unprobed', 'priority': 0,
    })

    normalized = normalize_document(document)
    canonical = {'creator_id': 'xai', 'model_id': model_id}
    models = [row for row in normalized['models'] if row['creator_id'] == 'xai']
    assert len(models) == 1
    assert all(models[0][key] == value for key, value in {
        **canonical, 'display_name': model_id, 'capabilities': ['text'],
        'context_window': 500_000, 'quality_rank': 1.0,
        'registration_state': 'pending_metadata',
    }.items())
    assert ('inference_profile' in models[0]) == (model_id == 'grok-4.7')
    assert next(row for row in normalized['offerings']
                if row['offering_id'] == 'grok-offering')['model'] == canonical
    assert canonical in normalized['credentials'][0]['authorization']['models']
    assert next(row for row in normalized['deployments']
                if row['deployment_id'] == 'grok-deployment')['wire_model_id'] == prefixed
    assert normalize_document(normalized) == normalized
    _validator().validate(normalized)


def test_release_date_is_projected_at_read_boundary_never_persisted() -> None:
    document = _full_document()
    for row in document['models']:
        if row['model_id'] == 'alpha':
            row['model_id'] = 'glm-5.3'
            # A client echoing the projected field back must not persist it.
            row['release_date'] = '1999-01'
    for credential in document['credentials']:
        for ref in credential['authorization']['models']:
            if ref['model_id'] == 'alpha':
                ref['model_id'] = 'glm-5.3'
    for offering in document['offerings']:
        if offering['model']['model_id'] == 'alpha':
            offering['model']['model_id'] = 'glm-5.3'

    normalized = normalize_document(document)
    assert all('release_date' not in model for model in normalized['models'])
    _validator().validate(normalized)

    projected = public_projection(document)
    by_id = {model['model_id']: model for model in projected['models']}
    assert by_id['glm-5.3']['release_date'] == '2026-08'
    assert 'release_date' not in by_id['beta']
    _validator().validate(projected)


def test_official_pricing_is_projected_at_read_boundary_never_persisted() -> None:
    document = _full_document()
    for row in document['models']:
        if row['model_id'] == 'alpha':
            row['model_id'] = 'claude-fable-5-1'
            # A client echoing the projected field back must not persist it.
            row['official_pricing'] = {
                'input': 999, 'output': 999, 'currency': 'USD',
                'unit': 'per_million_tokens',
            }
        if row['model_id'] == 'beta':
            row['model_id'] = 'gemini-3.8-flash'
            row['list_pricing'] = {
                'input': 3, 'output': 9, 'currency': 'USD',
                'unit': 'per_million_tokens',
            }
    for credential in document['credentials']:
        for ref in credential['authorization']['models']:
            if ref['model_id'] == 'alpha':
                ref['model_id'] = 'claude-fable-5-1'
            if ref['model_id'] == 'beta':
                ref['model_id'] = 'gemini-3.8-flash'
    for offering in document['offerings']:
        if offering['model']['model_id'] == 'alpha':
            offering['model']['model_id'] = 'claude-fable-5-1'
        if offering['model']['model_id'] == 'beta':
            offering['model']['model_id'] = 'gemini-3.8-flash'

    normalized = normalize_document(document)
    assert all('official_pricing' not in model for model in normalized['models'])
    by_id_normalized = {model['model_id']: model for model in normalized['models']}
    assert by_id_normalized['gemini-3.8-flash']['list_pricing']['input'] == 3
    _validator().validate(normalized)

    projected = public_projection(document)
    by_id = {model['model_id']: model for model in projected['models']}
    official = by_id['claude-fable-5-1']['official_pricing']
    assert official['input'] == 10.0
    assert official['output'] == 50.0
    assert official['cache_read'] == 0.25
    # A registered list_pricing stays authoritative — no projection stamped.
    assert 'official_pricing' not in by_id['gemini-3.8-flash']
    _validator().validate(projected)


def test_trained_model_alias_rows_merge_and_repoint_references() -> None:
    """Provider respellings (relay SKU, quant, snapshot) of one trained model
    collapse into the survivor row; offerings, credential authorizations and
    stale pins all follow the merge."""
    survivor = {'creator_id': 'creator', 'model_id': 'llama-4-maverick-17b-128e-instruct'}
    relay = {'creator_id': 'creator', 'model_id': 'cerebras-llama-4-maverick-17b-128e-instruct'}
    quant = {'creator_id': 'creator', 'model_id': 'llama-4-maverick-17b-128e-instruct-fp8'}
    document = _full_document()
    document['models'].append({
        **survivor, 'display_name': 'Llama 4 Maverick',
        'capabilities': ['text'], 'context_window': 128_000, 'quality_rank': 90,
    })
    document['models'].append({
        **relay, 'display_name': 'Cerebras Llama 4 Maverick',
        'capabilities': ['text', 'vision'], 'context_window': 128_000,
        'quality_rank': 90,
    })
    document['models'].append({
        **quant, 'display_name': 'Llama 4 Maverick FP8',
        'capabilities': ['text'], 'context_window': 1_000_000, 'quality_rank': 90,
    })
    document['offerings'][0]['model'] = copy.deepcopy(relay)
    document['offerings'][0]['capabilities'] = ['text', 'vision']
    document['credentials'][0]['authorization']['models'] = [
        copy.deepcopy(quant), copy.deepcopy(relay),
    ]

    normalized = normalize_document(document)

    model_ids = [row['model_id'] for row in normalized['models']]
    assert 'llama-4-maverick-17b-128e-instruct' in model_ids
    assert relay['model_id'] not in model_ids
    assert quant['model_id'] not in model_ids
    merged = next(
        row for row in normalized['models']
        if row['model_id'] == survivor['model_id'])
    assert merged['capabilities'] == ['text', 'vision']
    assert merged['context_window'] == 1_000_000
    assert normalized['offerings'][0]['model'] == survivor
    assert normalized['credentials'][0]['authorization']['models'] == [survivor]

    # Stale pins and provider respellings still resolve to the survivor.
    pinned = NativeModelSelection(ModelRef(**relay), None, '')
    assert compile_candidates(normalized, pinned)
    selection = resolve_compatible_model(
        normalized, 'cerebras-llama-4-maverick-17b-128e-instruct')
    assert selection.model == ModelRef(**survivor)


def test_offering_pricing_tier_tags_are_not_identity_capabilities() -> None:
    """The ``cheap`` tier derives from each provider's own price book, so a
    reseller offering may carry it even when the official model row (priced
    at the creator's list rates) does not.  Only true capability claims
    (vision on a text model) stay rejectable identity expansion."""
    document = _full_document()
    # Model alpha is ['text', 'thinking']; provider B undercuts the price.
    document['offerings'][1]['capabilities'] = ['text', 'cheap']
    normalized = normalize_document(document)
    offering = next(row for row in normalized['offerings']
                    if row['offering_id'] == 'alpha-b')
    assert offering['capabilities'] == ['cheap', 'text']

    document = _full_document()
    document['offerings'][1]['capabilities'] = ['text', 'vision']
    with pytest.raises(ModelRoutingError) as error:
        normalize_document(document)
    assert error.value.kind == 'offering_capability_expansion'
    assert 'vision' in str(error.value)


def test_desktop_adapter_connection_is_bounded_to_matching_loopback_port() -> None:
    document = _full_document()
    document['connections'][0].update({
        'base_url': 'http://127.0.0.1:8317/v1',
        'protocol': 'openai',
        'adapter': {'agent_id': 'agent-123', 'port': 8317},
    })

    normalized = normalize_document(document)
    _validator().validate(normalized)
    assert normalized['connections'][0]['adapter'] == {
        'agent_id': 'agent-123', 'port': 8317}

    for invalid in (
        {'base_url': 'http://127.0.0.1:9000/v1'},
        {'base_url': 'https://127.0.0.1:8317/v1'},
        {'base_url': 'http://remote.example:8317/v1'},
        {'protocol': 'anthropic'},
        {'adapter': {'agent_id': 'agent-123', 'port': 0}},
        {'adapter': {'agent_id': 'agent-123', 'port': 8317, 'extra': True}},
    ):
        candidate = copy.deepcopy(document)
        candidate['connections'][0].update(invalid)
        with pytest.raises(ModelRoutingError):
            normalize_document(candidate)


def test_legacy_adapter_marker_migrates_to_connection_transport() -> None:
    plan = plan_legacy_migration({'providers': [{
        'id': 'adapter_agent',
        'name': 'Desktop adapter',
        'base_url': 'http://127.0.0.1:8317/v1',
        'protocol': 'openai',
        'adapter': {'agent_id': 'agent-123', 'port': 8317},
        'api_keys': ['adapter-key'],
        'models': [{'model_id': 'adapter-model'}],
    }]})

    assert not plan.blocking_issues
    assert plan.document['connections'][0]['adapter'] == {
        'agent_id': 'agent-123', 'port': 8317}
    assert 'adapter-key' not in json.dumps(plan.public_dict())


def test_legacy_extra_headers_split_between_connection_and_envelope() -> None:
    plan = plan_legacy_migration({'providers': [{
        'id': 'gw',
        'name': 'Gateway',
        'base_url': 'https://gw.example/v1',
        'protocol': 'openai',
        'api_keys': ['sk-gateway-key-1234'],
        'extra_headers': {
            'M-Transfer-Cell': 'gray',
            'api-key': 'header-secret',
            'Host': 'gw.example',
        },
        'models': [{'model_id': 'gw-model'}],
    }]})

    assert not plan.blocking_issues
    connection = plan.document['connections'][0]
    assert connection['extra_headers'] == {'M-Transfer-Cell': 'gray'}
    envelope = json.loads(plan._secrets[0].plaintext)
    assert envelope['extra_headers'] == {
        'api-key': 'header-secret',
        'Host': 'gw.example',
    }
    assert plan.document['credentials'][0]['key_hint'] == 'sk-g…1234'
    # The plain routing header is inspectable document metadata (asserted on
    # the connection above); credential/reserved headers never leak into the
    # public projection or the persisted document.
    public = json.dumps(plan.public_dict())
    persisted = json.dumps(plan.document)
    assert 'header-secret' not in public
    assert 'header-secret' not in persisted


def test_secret_hint_derives_from_usable_key_not_envelope() -> None:
    repository = InMemoryModelRoutingRepository()
    owner = OwnerBoundary.create(21)
    envelope = json.dumps({
        'format': 'tofu.credential-secret/v1',
        'api_key': 'sk-usable-key-9876',
        'oauth': '',
        'extra_headers': {'api-key': 'header-secret'},
    }, separators=(',', ':'), sort_keys=True)
    stored = repository.put_secret(owner, envelope)
    assert stored['key_hint'] == secret_hint('sk-usable-key-9876')
    assert not stored['key_hint'].startswith('{')
    assert 'header-secret' not in stored['key_hint']

    raw = repository.put_secret(owner, 'sk-raw-key')
    assert raw['key_hint'] == secret_hint('sk-raw-key')

    headers_only = repository.put_secret(owner, json.dumps({
        'format': 'tofu.credential-secret/v1',
        'api_key': '',
        'oauth': '',
        'extra_headers': {'X-Tenant': 'blue'},
    }, separators=(',', ':'), sort_keys=True))
    assert headers_only['key_hint'] == 'headers'


def test_header_secrecy_classifier_keeps_credential_material_encrypted() -> None:
    assert is_credential_header('api-key')
    assert is_credential_header('X-Auth-Token')
    assert is_credential_header('Authorization')
    assert not is_credential_header('X-Custom-Header')
    envelope, connection = split_header_secrecy({
        'M-Cell': 'gray',
        'api-key': 's',
        'Host': 'gw.example',
        'X-Tenant': 'blue',
    })
    assert envelope == {'api-key': 's', 'Host': 'gw.example'}
    assert connection == {'M-Cell': 'gray', 'X-Tenant': 'blue'}


def test_owner_repository_isolation_revision_cas_and_secret_redaction() -> None:
    repository = InMemoryModelRoutingRepository()
    alice = OwnerBoundary.create(11, 'tenant-a')
    bob = OwnerBoundary.create(12, 'tenant-a')
    stored = repository.put_secret(alice, 'sk-owner-secret')
    document = _full_document()
    document['credentials'][0].update({
        'kind': 'api_key',
        'secret_reference': stored['secret_reference'],
        'key_hint': stored['key_hint'],
    })
    committed = repository.compare_and_swap(
        alice, document, expected_revision=0)

    assert committed.revision == 1
    assert repository.get(bob).revision == 0
    assert repository.resolve_secret(alice, stored['secret_reference']) == 'sk-owner-secret'
    assert 'sk-owner-secret' not in json.dumps(committed.public_document())
    with pytest.raises(ModelRoutingError) as error:
        repository.compare_and_swap(alice, document, expected_revision=0)
    assert error.value.kind == 'model_routing_revision_conflict'
    with pytest.raises(ModelRoutingError) as error:
        repository.resolve_secret(bob, stored['secret_reference'])
    assert error.value.kind == 'credential_secret_missing'


def test_sidecar_repository_translates_storage_failure_at_domain_boundary() -> None:
    from lib.model_routing import ModelRoutingRepository
    from lib.storage.errors import StorageError

    class _UnavailableClient:
        def query(self, _operation, _payload):
            raise StorageError('database_unavailable', 'sidecar offline')

    repository = ModelRoutingRepository()
    repository._client = lambda **_kwargs: _UnavailableClient()  # type: ignore[method-assign]

    with pytest.raises(ModelRoutingError) as error:
        repository.get(OwnerBoundary.create(11, 'tenant-a'))
    assert error.value.kind == 'model_routing_storage_unavailable'
    assert isinstance(error.value.__cause__, StorageError)


@pytest.mark.parametrize('forbidden_field', ['api_key', 'routes'])
def test_domain_rejects_secret_and_retired_routing_state(
    forbidden_field: str,
) -> None:
    document = empty_document()
    document['migration'] = {forbidden_field: 'must-not-survive'}

    with pytest.raises(ModelRoutingError):
        normalize_document(document)


def test_native_selection_has_structured_identity_and_separate_preference() -> None:
    selection = parse_native_model_selection({
        'model': {'creator_id': 'moonshot', 'model_id': 'kimi-k3'},
        'routing': {'preferred_provider_id': 'provider-a'},
    })

    assert selection.model is not None
    assert selection.model.public_dict() == {
        'creator_id': 'moonshot',
        'model_id': 'kimi-k3',
    }
    assert selection.preferred_provider_id == 'provider-a'
    with pytest.raises(ModelRoutingError, match='model@provider selectors'):
        parse_native_model_selection({'model': 'kimi-k3@provider-a'})


def test_routing_preference_filters_authorization_health_capability_and_budget() -> None:
    document = _full_document()
    for deployment in document['deployments']:
        deployment['enabled'] = False
        deployment['probe_status'] = 'unprobed'
    document = normalize_document(document)
    selection = NativeModelSelection(
        ModelRef('creator', 'alpha'), None, 'provider-b')
    candidates = compile_candidates(document, selection)
    assert [candidate.provider_id for candidate in candidates[:2]] == [
        'provider-b', 'provider-a',
    ]

    thinking = compile_candidates(document, selection, policy=RoutePolicy(
        required_capabilities=frozenset({'text', 'thinking'})))
    assert [candidate.provider_id for candidate in thinking] == ['provider-a']
    context = compile_candidates(document, selection, policy=RoutePolicy(
        required_context=150_000))
    assert [candidate.provider_id for candidate in context] == ['provider-a']
    price = compile_candidates(document, selection, policy=RoutePolicy(
        max_input_price=3, max_output_price=5))
    assert [candidate.provider_id for candidate in price] == ['provider-b']

    clock = [100.0]
    health = RouteHealthRegistry(clock=lambda: clock[0])
    health.record_candidate_failure(
        candidates[0], kind='network', reason='connection timeout')
    after_failure = compile_candidates(document, selection, health=health)
    assert [candidate.provider_id for candidate in after_failure] == ['provider-a']
    # A connection fault does not disable the other ProviderAccess, and its
    # bounded entry disappears after TTL.
    assert health.snapshot()['entries'][0]['scope'] == 'connection'
    clock[0] += 3601
    assert health.snapshot()['count'] == 0


def test_wire_exclusion_withholds_single_request_name() -> None:
    # alpha is deployed on access-a under two wire ids sharing one ModelRef.
    document = _full_document()
    document['deployments'].append({
        'deployment_id': 'alpha-a2-deployment', 'offering_id': 'alpha-a',
        'connection_id': 'connection-a', 'wire_model_id': 'alpha-wire-a2',
        'enabled': True, 'identity_confidence': 'high',
        'probe_status': 'passed', 'priority': 0,
    })
    # Exclude only one of the two wire ids on credential-a's alpha grant.
    grant = document['credentials'][0]['authorization']['models'][0]
    assert grant['model_id'] == 'alpha'
    grant['excluded_wire_model_ids'] = ['alpha-wire-a2']
    document = normalize_document(document)
    _validator().validate(document)

    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, None)
    wires = {c.deployment['wire_model_id']
             for c in compile_candidates(document, selection)
             if c.credential['credential_id'] == 'credential-a'}
    assert 'alpha-wire-a2' not in wires
    assert 'alpha-wire-a' in wires

    # Excluding every wire of the model makes the credential unreachable for
    # it, while the other credential's grant is untouched.
    document['credentials'][0]['authorization']['models'][0][
        'excluded_wire_model_ids'] = ['alpha-wire-a', 'alpha-wire-a2']
    document = normalize_document(document)
    wires = {c.deployment['wire_model_id']
             for c in compile_candidates(document, selection)
             if c.credential['credential_id'] == 'credential-a'}
    assert not wires
    assert any(c.credential['credential_id'] == 'credential-b'
               for c in compile_candidates(document, selection))


def test_wire_exclusion_unknown_ids_are_inert_and_survive_normalization() -> None:
    document = _full_document()
    grant = document['credentials'][0]['authorization']['models'][0]
    grant['excluded_wire_model_ids'] = ['alpha-wire-a', 'deleted-request-name']
    normalized = normalize_document(document)
    saved = normalized['credentials'][0]['authorization']['models'][0]
    # Unknown wires (a deleted request name) stay recorded but never match a
    # live deployment, so they cannot leak into another access.
    assert saved['excluded_wire_model_ids'] == ['alpha-wire-a', 'deleted-request-name']


def test_wire_exclusion_duplicate_grant_still_rejected() -> None:
    document = _full_document()
    dup = copy.deepcopy(document['credentials'][0]['authorization']['models'][0])
    dup['excluded_wire_model_ids'] = ['alpha-wire-a']
    document['credentials'][0]['authorization']['models'].append(dup)
    with pytest.raises(ModelRoutingError):
        normalize_document(document)


def test_model_fallback_prefers_same_provider_then_highest_quality() -> None:
    document = normalize_document(_full_document())
    selection = NativeModelSelection(
        ModelRef('creator', 'alpha'), None, 'provider-a')
    fallbacks = compile_model_fallback_candidates(
        document, selection, original_provider_id='provider-a')
    assert fallbacks
    assert fallbacks[0].provider_id == 'provider-a'
    assert fallbacks[0].model['model_id'] == 'beta'
    snapshot = RouteSnapshotBuilder(selection)
    primary = compile_candidates(document, selection)[0]
    snapshot.record_transition(
        source=primary, target=fallbacks[0],
        reason='all alpha deployments failed', kind='model_fallback')
    projected = snapshot.finalize(fallbacks[0])
    assert projected['credential'] == {
        'credential_id': 'credential-a', 'kind': 'local_identity', 'key_hint': '',
    }
    assert 'secret' not in json.dumps(projected)
    assert len(json.dumps(projected).encode()) <= 16 * 1024


def test_catalog_health_and_route_snapshot_resource_budgets() -> None:
    oversized = empty_document()
    oversized['creators'] = [{}] * (MAX_COUNTS['creators'] + 1)
    with pytest.raises(ModelRoutingError, match='resource budget') as error:
        normalize_document(oversized)
    assert error.value.field == 'creators'

    clock = [10.0]
    health = RouteHealthRegistry(
        max_entries=64, ttl_seconds=60, clock=lambda: clock[0])
    for index in range(80):
        health.record_failure(
            HealthTarget('deployment', f'deployment-{index}'),
            reason='bounded-capacity')
    snapshot = health.snapshot(limit=1_000)
    assert snapshot['capacity'] == 64
    assert snapshot['count'] == 64
    assert len(snapshot['entries']) == 64
    assert snapshot['entries'][0]['entity_id'] == 'deployment-16'
    clock[0] += 61
    assert health.snapshot()['count'] == 0

    document = normalize_document(_full_document())
    selection = NativeModelSelection(
        ModelRef('creator', 'alpha'), None, 'provider-a')
    candidate = compile_candidates(document, selection)[0]
    builder = RouteSnapshotBuilder(selection)
    for index in range(64):
        builder.record_transition(
            source=candidate,
            target=candidate,
            reason=f'{index}-' + ('reason' * 300),
            kind='provider_failover')
    for index in range(32):
        builder.record_degradation(f'{index}-' + ('degraded' * 300))
    route_snapshot = builder.finalize(candidate)
    assert len(json.dumps(route_snapshot, ensure_ascii=False).encode()) \
        <= MAX_ROUTE_SNAPSHOT_BYTES
    assert len(route_snapshot['transitions']) <= 32
    assert len(route_snapshot['degradation_reasons']) <= 16


def test_migration_maps_faces_keys_access_wire_ids_and_pending_identity() -> None:
    official = [{
        'creator_id': 'creator', 'model_id': 'alpha', 'display_name': 'Alpha',
        'capabilities': ['text', 'thinking'], 'context_window': 200_000,
        'quality_rank': 100,
    }]
    legacy = {'providers': [{
        'id': 'gateway', 'name': 'Gateway',
        'base_url': 'https://gateway.example/v1',
        'faces': {
            'anthropic': {
                'base_url': 'https://gateway.example/anthropic',
                'protocol': 'anthropic',
            },
        },
        'endpoints': ['https://backup.example/v1'],
        'api_keys': ['sk-first-secret', 'sk-second-secret'],
        'extra_headers': {'X-Private-Token': 'header-secret'},
        'models': [{
            'model_id': 'alpha', 'capabilities': ['text'],
            'context_window': 100_000,
            'request_ids': ['alpha-wire', 'alpha-region-wire'],
            'key_access': {'0': {}, '1': {'disabled': True}},
        }, {
            'model_id': 'unconfirmed-preview', 'aliases': ['pending-wire'],
            'capabilities': ['text'],
        }],
    }]}
    plan = plan_legacy_migration(legacy, official_directory=official)
    assert not plan.blocking_issues
    assert len(plan.document['connections']) == 3
    assert len(plan.document['credentials']) == 2
    assert {row['wire_model_id'] for row in plan.document['deployments']} == {
        'alpha-wire', 'alpha-region-wire',
        'unconfirmed-preview', 'pending-wire',
    }
    pending = next(row for row in plan.document['offerings']
                   if row['identity_state'] == 'pending_identity')
    assert pending['enabled'] is True
    assert all('enabled' not in row for row in plan.document['deployments'])
    assert all(row['probe_status'] == 'unprobed'
               for row in plan.document['deployments'])
    assert all(row['extra_headers'] == {} for row in plan.document['connections'])
    public_plan = json.dumps(plan.public_dict())
    assert 'sk-first-secret' not in public_plan
    assert 'header-secret' not in public_plan

    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(21)
    result = execute_migration(repository, boundary, plan, now=lambda: 123.0)
    assert result.enabled is True
    assert repository.get(boundary).revision == 1
    assert repository.migration_receipt(boundary)['receipt']['status'] == 'committed'


def test_migration_folds_dated_pin_list_pricing_to_rolling_alias() -> None:
    legacy = {'providers': [{
        'id': 'gateway', 'name': 'Gateway',
        'base_url': 'https://gateway.example/v1',
        'models': [
            {'model_id': 'deepseek-v4-pro',
             'pricing': {'input': 1.32, 'output': 3.96, 'cache_read': 0.044}},
            # Scan materialized explicit zeros for the pin: the catalog had
            # no price card for the dated id, but $0 is not an official tier.
            {'model_id': 'deepseek-v4-pro-0813',
             'pricing': {'input': 0.0, 'output': 0.0}},
            {'model_id': 'kimi-k2-instruct-0905',
             'pricing': {'input': 0.0, 'output': 0.0}},
            {'model_id': 'glm-9.9',
             'pricing': {'input': 0.0, 'output': 0.0}},
        ],
    }]}
    plan = plan_legacy_migration(legacy)

    assert not plan.blocking_issues
    by_id = {row['model_id']: row for row in plan.document['models']}
    folded = by_id['deepseek-v4-pro-0813']['list_pricing']
    assert folded['input'] == 1.32
    assert folded['output'] == 3.96
    assert folded['cache_read'] == 0.044
    assert by_id['deepseek-v4-pro']['list_pricing']['input'] == 1.32
    # No priced rolling alias in-document: the placeholder zero is not an
    # official price, so the pin stays unpriced (目录未挂牌), not $0.
    assert 'list_pricing' not in by_id['kimi-k2-instruct-0905']
    # Same for a non-pin model: all-zero list pricing never materializes.
    assert 'list_pricing' not in by_id['glm-9.9']


def test_failed_migration_keeps_authority_unchanged_and_receipt_durable() -> None:
    legacy = {'providers': [{
        'id': 'duplicate-wire', 'name': 'Duplicate wire',
        'base_url': 'https://gateway.example/v1',
        'api_key': 'secret',
        'models': [
            {'model_id': 'custom-a', 'request_ids': ['same-wire']},
            {'model_id': 'custom-b', 'request_ids': ['same-wire']},
        ],
    }]}
    plan = plan_legacy_migration(legacy)
    assert plan.blocking_issues
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(22)
    result = execute_migration(repository, boundary, plan, now=lambda: 456.0)

    assert result.enabled is False
    assert repository.get(boundary).revision == 0
    assert repository.migration_receipt(boundary)['receipt']['status'] == 'rejected'
    with pytest.raises(ModelRoutingError, match='inline provider blocks'):
        parse_native_model_selection({
            'model': {'creator_id': 'moonshot', 'model_id': 'kimi-k3'},
            'provider': {'api_key': 'must-not-be-accepted'},
        })


def test_encrypted_credential_headers_share_reserved_header_guard() -> None:
    from lib.model_routing.dispatch_adapter import decode_credential_secret

    envelope = json.dumps({
        'format': 'tofu.credential-secret/v1',
        'api_key': 'secret',
        'extra_headers': {'Authorization': 'Bearer attacker'},
    })
    with pytest.raises(ModelRoutingError, match='reserved'):
        decode_credential_secret(envelope, kind='api_key')


def test_schema_54_migrates_model_routing_authority_and_secret_tables() -> None:
    """A v53 authority gains v2 stores and publishes the current version."""
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.schema import SCHEMA_VERSION, initialize_schema

    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    connection.execute('DROP TABLE storage_model_routing_secrets')
    connection.execute('DROP TABLE storage_model_routing_authorities')
    connection.execute(
        "UPDATE storage_meta SET meta_value = '53' "
        "WHERE meta_key = 'schema_version'"
    )

    initialize_schema(session)

    version = connection.execute(
        "SELECT meta_value FROM storage_meta WHERE meta_key = 'schema_version'"
    ).fetchone()[0]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert int(version) == SCHEMA_VERSION
    assert {
        'storage_model_routing_authorities',
        'storage_model_routing_secrets',
    } <= tables


def test_storage_commit_acknowledgement_stays_receipt_small_for_large_authority() -> None:
    from lib.storage_sidecar.adapters.sqlite import SQLiteSession
    from lib.storage_sidecar.operations_pkg._model_routing import (
        _model_routing_commit,
    )
    from lib.storage_sidecar.receipt_codec import encode_receipt_response
    from lib.storage_sidecar.schema import initialize_schema

    connection = sqlite3.connect(':memory:')
    connection.row_factory = sqlite3.Row
    session = SQLiteSession(connection)
    initialize_schema(session)
    large_document = empty_document(revision=1)
    # This operation receives an already domain-validated aggregate. Padding
    # reproduces the storage/receipt size boundary without unrelated fixtures.
    large_document['diagnostic_padding'] = [
        f'{index:06d}-' + ('abcdef0123456789' * 12)
        for index in range(1800)
    ]

    response = _model_routing_commit(session, {
        'owner_user_id': 1,
        'tenant_id': '',
        'expected_revision': 0,
        'document': large_document,
        'updated_at': 123.0,
    })

    assert response['revision'] == 1
    assert 'document' not in response
    assert len(encode_receipt_response(response)) < 1024
    stored = connection.execute(
        'SELECT length(document_json) FROM storage_model_routing_authorities'
    ).fetchone()[0]
    assert stored > 300_000


@pytest.mark.parametrize('legacy_enabled', [True, False, None])
@pytest.mark.parametrize('probe_status', ['unprobed', 'failed', 'stale', 'passed'])
def test_request_name_diagnostics_do_not_gate_confirmed_routes(
    legacy_enabled, probe_status,
) -> None:
    document = _full_document()
    document['revision'] = 17
    for deployment in document['deployments']:
        deployment.pop('enabled')
        if legacy_enabled is not None:
            deployment['enabled'] = legacy_enabled
        deployment['probe_status'] = probe_status
        deployment['identity_confidence'] = 'pending'
    original = copy.deepcopy(document)
    normalized = normalize_document(document)
    assert document == original
    assert normalized['revision'] == 17
    assert all('enabled' not in row for row in normalized['deployments'])
    assert normalize_document(normalized) == normalized
    assert public_projection(document)['deployments'] == normalized['deployments']
    assert normalized['credentials'] == document['credentials']
    assert len(compile_candidates(
        document, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''),
    )) == 2
    _validator().validate(normalized)


@pytest.mark.parametrize('malformed', [0, 1, '', 'false', None, [], {}])
def test_legacy_request_name_enable_requires_boolean(malformed) -> None:
    document = _full_document()
    document['deployments'][0]['enabled'] = malformed
    with pytest.raises(ModelRoutingError) as error:
        normalize_document(document)
    assert error.value.field == 'deployments[0].enabled'


@pytest.mark.parametrize('collection', [
    'provider_accesses', 'connections', 'credentials', 'offerings',
])
def test_visible_switches_still_disable_unprobed_legacy_false_routes(collection) -> None:
    document = _full_document()
    for deployment in document['deployments']:
        deployment['enabled'] = False
        deployment['probe_status'] = 'unprobed'
    document[collection][0]['enabled'] = False
    candidates = compile_candidates(
        document, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''),
    )
    assert [candidate.provider_id for candidate in candidates] == ['provider-b']


@pytest.mark.parametrize('grant', ['models', 'connection_ids'])
def test_grants_still_limit_unprobed_legacy_false_routes(grant) -> None:
    document = _full_document()
    for deployment in document['deployments']:
        deployment['enabled'] = False
        deployment['probe_status'] = 'unprobed'
    document['credentials'][0]['authorization'][grant] = []
    candidates = compile_candidates(
        document, NativeModelSelection(ModelRef('creator', 'alpha'), None, ''),
    )
    assert [candidate.provider_id for candidate in candidates] == ['provider-b']


@pytest.mark.parametrize('gate,expected', [
    ('offering', 'offering_disabled'),
    ('connection', 'connection_disabled'),
    ('credential', 'credential_disabled'),
    ('model_grant', 'credential_model_denied'),
    ('connection_grant', 'credential_connection_denied'),
    ('capabilities', 'capabilities_missing'),
    ('context', 'context_insufficient'),
    ('supply', 'offering_missing'),
])
def test_route_rejections_explain_the_actual_eligibility_gate(gate, expected):
    doc = _full_document()
    policy = RoutePolicy()
    if gate in {'offering', 'connection', 'credential'}:
        for row in doc[gate + 's']:
            row['enabled'] = False
    elif gate == 'model_grant':
        for row in doc['credentials']:
            row['authorization']['models'] = []
    elif gate == 'connection_grant':
        for row in doc['credentials']:
            row['authorization']['connection_ids'] = []
    elif gate == 'capabilities':
        policy = RoutePolicy(required_capabilities=frozenset({'vision'}))
    elif gate == 'context':
        policy = RoutePolicy(required_context=1_000_000)
    else:
        doc['offerings'] = []
        doc['deployments'] = []
    rejected = {}
    candidates = compile_candidates(doc, NativeModelSelection(ModelRef('creator', 'alpha'), None),
                                    policy=policy, rejections=rejected)
    assert candidates == []
    assert rejected.get(expected, 0) > 0


def test_unavailable_route_surfaces_safe_authorization_diagnostics():
    from types import SimpleNamespace
    from lib.model_routing.dispatch_adapter import mint_routed_slot_group
    doc = _full_document()
    for credential in doc['credentials']:
        credential['authorization']['models'] = []
    repository = SimpleNamespace(get=lambda owner: SimpleNamespace(document=doc))
    with pytest.raises(ModelRoutingError, match='credential_model_denied') as captured:
        mint_routed_slot_group(repository, OwnerBoundary.create(23),
                               NativeModelSelection(ModelRef('creator', 'alpha'), None))
    assert captured.value.kind == 'model_route_unavailable'
    assert 'secret_reference' not in str(captured.value)
    assert 'https://' not in str(captured.value)


def _candidate_product_document(deployments=100, credentials=200):
    document = _full_document()
    base_deployment = document['deployments'][0]
    document['deployments'].extend([
        dict(base_deployment, deployment_id=f'product-deployment-{i:04d}',
             wire_model_id=f'product-wire-{i:04d}', priority=i % 7)
        for i in range(deployments - 1)])
    base_credential = document['credentials'][0]
    document['credentials'].extend([
        dict(base_credential, credential_id=f'product-credential-{i:04d}')
        for i in range(credentials - 1)])
    return document


def test_bounded_candidate_ranking_matches_full_prefix_with_measured_heap():
    import gc
    import tracemalloc
    from lib.model_routing.routing import RouteCandidateCompiler
    compiler = RouteCandidateCompiler(_candidate_product_document())
    selection = NativeModelSelection(ModelRef('creator', 'alpha'), None, 'provider-a')

    def measure(limit):
        gc.collect()
        tracemalloc.start()
        try:
            candidates = compiler.compile(selection, max_candidates=limit)
            peak = tracemalloc.get_traced_memory()[1]
            identities = [(c.deployment['deployment_id'], c.credential['credential_id'])
                          for c in candidates]
            return identities, peak
        finally:
            tracemalloc.stop()

    all_ids, full_peak = measure(None)
    bounded_ids, bounded_peak = measure(64)
    assert len(all_ids) == 20_001
    assert bounded_ids == all_ids[:64]
    assert bounded_peak < 512 * 1024
    assert bounded_peak * 10 < full_peak


@pytest.mark.parametrize('limit', [1, 2, 5, 64])
def test_bounded_fallback_order_matches_full_ranking(limit):
    document = _candidate_product_document(deployments=8, credentials=10)
    # Starting from beta leaves the product-sized alpha alternatives eligible.
    selection = NativeModelSelection(ModelRef('creator', 'beta'), None, 'provider-b')
    options = dict(original_provider_id='provider-b',
                   policy=RoutePolicy(required_capabilities=frozenset({'text'})))
    full = compile_model_fallback_candidates(document, selection, **options)
    bounded = compile_model_fallback_candidates(
        document, selection, max_candidates=limit, **options)
    assert bounded == full[:limit]


def test_fallback_excludes_resolved_identity_of_old_model_spelling():
    document = _full_document()
    selection = NativeModelSelection(ModelRef('creator', 'ALPHA'), None, '')
    assert compile_candidates(document, selection)
    candidates = compile_model_fallback_candidates(document, selection, max_candidates=1)
    assert len(candidates) == 1
    assert candidates[0].model['model_id'] == 'beta'


@pytest.mark.parametrize('limit', [0, -1, True, 1.5])
def test_candidate_limit_rejects_invalid_resource_budget(limit):
    with pytest.raises(ModelRoutingError, match='positive integer'):
        compile_candidates(_full_document(),
                           NativeModelSelection(ModelRef('creator', 'alpha'), None, ''),
                           max_candidates=limit)


def test_full_primary_prefix_does_not_compile_unused_fallbacks(monkeypatch):
    from lib.model_routing import dispatch_adapter as adapter
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(23)
    from tests.support.model_registration import complete_registration
    repository.compare_and_swap(boundary, complete_registration(_full_document()), expected_revision=0)
    monkeypatch.setattr(adapter, 'compile_model_fallback_candidates',
                        lambda *a, **kw: pytest.fail('unused fallback scan'))
    def reached_mint(**kwargs):
        raise RuntimeError('selected bounded candidate reached transport binding')
    monkeypatch.setattr(adapter, 'mint_ephemeral_slot', reached_mint)
    with pytest.raises(RuntimeError, match='reached transport binding'):
        adapter.mint_routed_slot_group(repository, boundary,
            NativeModelSelection(ModelRef('creator', 'alpha'), None, ''), max_candidates=1)


@pytest.mark.parametrize('entity', ['offerings', 'connections', 'deployments'])
def test_explicit_zero_priority_is_not_replaced_by_default(entity):
    document = _full_document()
    for name in ('offerings', 'connections', 'deployments'):
        for row in document[name]:
            row['priority'] = 10
    document[entity][0]['priority'] = 0
    document[entity][1]['priority'] = 1
    candidates = compile_candidates(document,
        NativeModelSelection(ModelRef('creator', 'alpha'), None, ''))
    assert [candidate.provider_id for candidate in candidates] == ['provider-a', 'provider-b']

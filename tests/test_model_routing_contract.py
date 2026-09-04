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
    plan_legacy_migration,
)


pytestmark = pytest.mark.unit


def _validator() -> Draft202012Validator:
    contract = (
        Path(__file__).parents[1]
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
    document = normalize_document(_full_document())
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
    assert pending['enabled'] is False
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

"""First-run provider staging enters the owner-scoped v2 authority once."""

from __future__ import annotations

import json

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    OwnerBoundary,
    empty_document,
)
from lib.model_routing import bootstrap as bootstrap_module

from lib.secret_envelope import secret_hint


pytestmark = pytest.mark.unit


def _pending(path) -> None:
    path.write_text(json.dumps({
        'contract_version': 'tofu.bootstrap-provider-stage/v1',
        'name': 'First Run',
        'brand': 'example',
        'base_url': 'https://models.example/v1',
        'protocol': 'openai',
        'credential_env': 'LLM_API_KEYS',
        'default_model': 'first-model',
        'models': [{
            'model_id': 'first-model',
            'capabilities': ['text', 'thinking'],
            'rpm': 20,
        }],
    }), encoding='utf-8')


def test_owner_bootstrap_activates_empty_authority_without_legacy_state(
        tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(71)
    pending_path = tmp_path / 'missing.json'
    monkeypatch.setattr(
        bootstrap_module, '_legacy_sources', lambda _boundary: ({}, []))
    monkeypatch.setattr(
        bootstrap_module, '_pending_provider_path', lambda: str(pending_path))

    result = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)

    assert result['status'] == 'initialized_empty'
    assert result['revision'] == 1
    assert repository.get(boundary).document['providers'] == []


def test_active_authority_consumes_secret_free_pending_provider(
        tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(72, 'tenant-a')
    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    pending_path = tmp_path / 'pending.json'
    _pending(pending_path)
    monkeypatch.setattr(
        bootstrap_module, '_pending_provider_path', lambda: str(pending_path))
    monkeypatch.setattr(
        bootstrap_module, '_legacy_sources', lambda _boundary: ({}, []))
    monkeypatch.setenv('LLM_API_KEYS', 'owner-secret,second-secret')

    result = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)

    assert result['status'] == 'already_active'
    assert result['pending_provider']['status'] == 'imported'
    assert not pending_path.exists()
    authority = repository.get(boundary)
    assert authority.revision == 2
    assert len(authority.document['providers']) == 1
    assert authority.document['providers'][0]['scope'] == 'owner'
    assert authority.document['offerings'][0]['pending_model_id'] == 'first-model'
    reference = authority.document['credentials'][0]['secret_reference']
    assert repository.resolve_secret(boundary, reference) == 'owner-secret'

    replay = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)
    assert replay == {
        'status': 'already_active',
        'revision': 2,
        'owner_user_id': 72,
    }


def test_missing_pending_credential_defers_without_losing_draft(
        tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(73)
    repository.compare_and_swap(
        boundary, empty_document(), expected_revision=0)
    pending_path = tmp_path / 'pending.json'
    _pending(pending_path)
    monkeypatch.setattr(
        bootstrap_module, '_pending_provider_path', lambda: str(pending_path))
    monkeypatch.setattr(
        bootstrap_module, '_legacy_sources', lambda _boundary: ({}, []))
    monkeypatch.delenv('LLM_API_KEYS', raising=False)

    result = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)

    assert result['pending_provider'] == {
        'status': 'deferred',
        'error_kind': 'bootstrap_provider_secret_missing',
    }
    assert pending_path.exists()
    assert repository.get(boundary).revision == 1


def test_active_oauth_authority_recovers_missing_legacy_provider_without_loss(
        tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(74)
    oauth_plan = bootstrap_module.plan_legacy_migration({'providers': [{
        'id': 'oauth_codex',
        'name': 'ChatGPT subscription',
        'brand': 'oauth',
        'oauth': 'codex',
        'base_url': 'https://chatgpt.com/backend-api/codex',
        'models': [{'model_id': 'gpt-5.6'}],
    }]})
    oauth_result = bootstrap_module.execute_migration(
        repository, boundary, oauth_plan)
    assert oauth_result.enabled
    active = repository.get(boundary)
    active.document.pop('migration', None)
    repository.compare_and_swap(
        boundary, active.document, expected_revision=active.revision)

    monkeypatch.setattr(
        bootstrap_module, '_legacy_sources', lambda _boundary: ({
            'providers': [{
                'id': 'default',
                'name': 'Default',
                'brand': 'yourprovider',
                'base_url': 'https://api.openai.com/v1',
                'faces': {
                    'anthropic': {
                        'base_url': 'https://api.openai.com/v1/anthropic',
                        'protocol': 'anthropic',
                    },
                },
                'api_keys': ['legacy-secret'],
                'models': [{
                    'model_id': 'LongCat-2.0',
                    'capabilities': ['text', 'thinking'],
                }],
            }],
        }, []))
    monkeypatch.setattr(
        bootstrap_module, '_pending_provider_path',
        lambda: str(tmp_path / 'missing.json'))

    result = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)

    assert result['status'] == 'recovered_legacy'
    authority = repository.get(boundary)
    assert {row['provider_id'] for row in authority.document['providers']} == {
        'oauth_codex', 'example-corp',
    }
    yourprovider_connections = [
        row for row in authority.document['connections']
        if 'your-llm-gateway.example.com' in row['base_url']
    ]
    assert {row['protocol'] for row in yourprovider_connections} == {
        'openai', 'anthropic',
    }


def _seed_leaked_envelope_authority(repository, boundary) -> None:
    """Recreate the early-v2 storage shape the repair pass targets.

    Writers of that era folded every provider header into the credential
    envelope, left the connection bare, and stored a hint derived from the
    whole envelope JSON.
    """
    plan = bootstrap_module.plan_legacy_migration({'providers': [{
        'id': 'yourprovider',
        'name': 'Default',
        'brand': 'yourprovider',
        'base_url': 'https://api.openai.com/v1/anthropic',
        'protocol': 'anthropic',
        'api_keys': ['00009999888877776666'],
        'extra_headers': {
            'X-Custom-Header': 'gray-release',
            'api-key': 'gateway-secret',
        },
        'models': [{'model_id': 'LongCat-2.0', 'capabilities': ['text']}],
    }]})
    assert not plan.blocking_issues
    old_envelope = json.dumps({
        'format': 'tofu.credential-secret/v1',
        'api_key': '00009999888877776666',
        'oauth': '',
        'extra_headers': {
            'X-Custom-Header': 'gray-release',
            'api-key': 'gateway-secret',
        },
    }, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    repository.put_secret(
        boundary, old_envelope,
        secret_reference=plan._secrets[0].secret_reference)
    document = plan.document
    document['connections'][0]['extra_headers'] = {}
    document['credentials'][0]['key_hint'] = '{"ap…ELL"'
    repository.compare_and_swap(boundary, document, expected_revision=0)


def test_bootstrap_repairs_envelope_storage_shape_and_hint(
        tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(75)
    _seed_leaked_envelope_authority(repository, boundary)
    monkeypatch.setattr(
        bootstrap_module, '_legacy_sources', lambda _boundary: ({}, []))
    monkeypatch.setattr(
        bootstrap_module, '_pending_provider_path',
        lambda: str(tmp_path / 'missing.json'))

    result = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)

    assert result['status'] == 'already_active'
    authority = repository.get(boundary)
    assert authority.document['connections'][0]['extra_headers'] == {
        'X-Custom-Header': 'gray-release'}
    credential = authority.document['credentials'][0]
    assert credential['key_hint'] == secret_hint('00009999888877776666')
    envelope = json.loads(repository.resolve_secret(
        boundary, credential['secret_reference']))
    assert envelope['api_key'] == '00009999888877776666'
    assert envelope['extra_headers'] == {'api-key': 'gateway-secret'}
    assert 'gateway-secret' not in json.dumps(authority.public_document())

    repaired_revision = authority.revision
    replay = bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)
    assert replay['status'] == 'already_active'
    assert repository.get(boundary).revision == repaired_revision


def test_bootstrap_repair_keeps_conflicted_headers_encrypted(
        tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(76)
    _seed_leaked_envelope_authority(repository, boundary)
    authority = repository.get(boundary)
    overridden = authority.document
    overridden['connections'][0]['extra_headers'] = {
        'X-Custom-Header': 'user-override'}
    repository.compare_and_swap(
        boundary, overridden, expected_revision=authority.revision)
    monkeypatch.setattr(
        bootstrap_module, '_legacy_sources', lambda _boundary: ({}, []))
    monkeypatch.setattr(
        bootstrap_module, '_pending_provider_path',
        lambda: str(tmp_path / 'missing.json'))

    bootstrap_module.bootstrap_owner_model_routing(
        boundary, repository=repository)

    authority = repository.get(boundary)
    # The user's connection-level override wins; the conflicted routing
    # header stays encrypted so the credential keeps its
    # envelope-over-connection dispatch precedence, and the hint is still
    # re-derived from the usable key.
    assert authority.document['connections'][0]['extra_headers'] == {
        'X-Custom-Header': 'user-override'}
    credential = authority.document['credentials'][0]
    envelope = json.loads(repository.resolve_secret(
        boundary, credential['secret_reference']))
    assert envelope['extra_headers'] == {
        'X-Custom-Header': 'gray-release',
        'api-key': 'gateway-secret',
    }
    assert credential['key_hint'] == secret_hint('00009999888877776666')

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

"""Carried provider authority: import once, re-seal secrets, never merge.

The bundle is what ``export.py --mode internal`` writes from the LIVE
model-routing authority. A fresh destination imports it at first boot; an
install that already has its own authority keeps it.
"""
from __future__ import annotations

import json

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    OwnerBoundary,
    empty_document,
)
from lib.model_routing import bootstrap as bootstrap_module
from lib.model_routing.carried_import import (
    CONTRACT_VERSION,
    import_carried_model_routing,
    read_carried_bundle,
)
from lib.model_routing.domain import ModelRoutingError

pytestmark = pytest.mark.unit


def _bundle(path, *, secret='carried-key'):
    payload = {
        'contract_version': CONTRACT_VERSION,
        'document': {
            **empty_document(),
            'providers': [{'provider_id': 'sankuai', 'name': 'Meituan',
                           'scope': 'public'}],
            'provider_accesses': [{
                'provider_access_id': 'access_1',
                'provider_id': 'sankuai',
                'display_name': 'Meituan',
                'enabled': True,
                'quota_policy': {},
            }],
            'credentials': [{
                'credential_id': 'cred_1',
                'provider_access_id': 'access_1',
                'kind': 'api_key',
                'enabled': True,
                'authorization': {'connection_ids': [], 'models': []},
                'quota_policy': {},
                'secret_reference': 'mrs_carried',
            }],
        },
        'secrets': {'cred_1': secret},
    }
    path.write_text(json.dumps(payload), encoding='utf-8')
    return path


def test_missing_bundle_is_a_no_op(tmp_path):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    assert import_carried_model_routing(repository, boundary,
                                        path=str(tmp_path / 'absent.json')) is None


def test_import_reseals_and_commits_then_removes_the_plaintext_bundle(tmp_path):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    bundle = _bundle(tmp_path / 'carried.json')

    result = import_carried_model_routing(repository, boundary, path=str(bundle))

    assert result['status'] == 'imported_carried_authority'
    assert result['credentials'] == 1
    authority = repository.get(boundary)
    assert [p['provider_id'] for p in authority.document['providers']] == ['sankuai']
    assert repository.resolve_secret(boundary, 'mrs_carried') == 'carried-key'
    assert not bundle.exists(), 'the plaintext bundle is consumed by a successful import'
    assert read_carried_bundle(str(bundle)) is None


def test_an_active_authority_is_never_overwritten(tmp_path):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    repository.compare_and_swap(boundary, empty_document(), expected_revision=0)
    bundle = _bundle(tmp_path / 'carried.json')

    result = import_carried_model_routing(repository, boundary, path=str(bundle))

    assert result['status'] == 'skipped_active_authority'
    assert repository.get(boundary).document['providers'] == []
    assert bundle.exists(), 'the bundle is left for the operator to inspect'


def test_a_bundle_missing_a_secret_fails_closed(tmp_path):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    bundle = _bundle(tmp_path / 'carried.json')
    payload = json.loads(bundle.read_text())
    payload['secrets'] = {}
    bundle.write_text(json.dumps(payload), encoding='utf-8')

    with pytest.raises(ModelRoutingError):
        import_carried_model_routing(repository, boundary, path=str(bundle))
    assert repository.get(boundary).revision == 0
    assert bundle.exists(), 'a failed import keeps the bundle so the next boot retries'


def test_an_unsupported_contract_is_rejected(tmp_path):
    path = tmp_path / 'carried.json'
    path.write_text(json.dumps({'contract_version': 'other', 'document': {},
                                'secrets': {}}), encoding='utf-8')
    with pytest.raises(ModelRoutingError):
        read_carried_bundle(str(path))


def test_bootstrap_prefers_the_carried_authority_over_legacy_providers(tmp_path, monkeypatch):
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(1)
    bundle = _bundle(tmp_path / 'carried.json')
    monkeypatch.setattr('lib.model_routing.carried_import.carried_path', lambda: str(bundle))
    # Legacy config still holds a DIFFERENT, stale provider list; it must not
    # be migrated when a live carried authority is present.
    monkeypatch.setattr(bootstrap_module, '_legacy_sources', lambda _boundary: (
        {'providers': [{'id': 'stale', 'name': 'Stale', 'api_keys': ['old-key']}]}, []))
    monkeypatch.setattr(bootstrap_module, '_pending_provider_path',
                        lambda: str(tmp_path / 'absent.json'))

    result = bootstrap_module.bootstrap_owner_model_routing(boundary, repository=repository)

    assert result['status'] == 'imported_carried_authority'
    providers = [p['provider_id'] for p in repository.get(boundary).document['providers']]
    assert providers == ['sankuai']
    assert repository.resolve_secret(boundary, 'mrs_carried') == 'carried-key'

"""One OAuth status response observes one owner routing revision without reuse across requests."""

from unittest import mock

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, OwnerBoundary
from lib.model_routing.routing import RouteCandidateCompiler
from lib.oauth import outbound
from lib.oauth.manager import _flow

pytestmark = pytest.mark.unit


@pytest.fixture
def routing(monkeypatch):
    repository = InMemoryModelRoutingRepository()
    monkeypatch.setattr('lib.oauth.token_store.load_token', lambda provider: {'access_token': 'test-token'})
    monkeypatch.setattr('lib.oauth.codex_catalog.cached_codex_provider_models', lambda: [])
    monkeypatch.setattr('lib.oauth.codex_catalog.codex_catalog_status', lambda: {'catalog_source': 'test'})
    monkeypatch.setattr(outbound, '_activate_oauth_config_change', lambda: None)
    monkeypatch.setattr(_flow, '_active_flows', {})
    for provider in outbound.OAUTH_PROVIDERS:
        outbound.provision_oauth_provider(provider, owner_user_id=41, repository=repository)
    monkeypatch.setattr('lib.model_routing.ModelRoutingRepository', lambda: repository)
    return repository


def test_all_provider_cards_share_one_authority_read_and_compiler(routing):
    expected = {provider: _flow.get_oauth_status(provider, owner_user_id=41)
                for provider in outbound.OAUTH_PROVIDERS}
    with mock.patch.object(routing, 'get', wraps=routing.get) as read, \
         mock.patch('lib.model_routing.routing.RouteCandidateCompiler', wraps=RouteCandidateCompiler) as compile_:
        actual = _flow.get_all_oauth_status(owner_user_id=41)
    assert actual == expected
    assert all(status['authenticated'] and status['provider_ready'] for status in actual.values())
    read.assert_called_once_with(OwnerBoundary.create(41))
    assert compile_.call_count == 1


def test_each_request_observes_revocation_and_owner_changes(routing):
    assert all(status['provider_ready'] for status in _flow.get_all_oauth_status(owner_user_id=41).values())
    boundary = OwnerBoundary.create(41)
    current = routing.get(boundary)
    for credential in current.document['credentials']:
        credential['enabled'] = False
    routing.compare_and_swap(boundary, current.document, expected_revision=current.revision)
    revoked = _flow.get_all_oauth_status(owner_user_id=41)
    foreign = _flow.get_all_oauth_status(owner_user_id=42)
    assert all(not status['provider_ready'] for status in revoked.values())
    assert all(not status['provider_ready'] and not status['model_ids'] for status in foreign.values())
    assert all(status['model_ids'] for status in revoked.values())


def test_failed_snapshot_is_consistent_and_next_request_recovers(routing):
    read = routing.get
    with mock.patch.object(routing, 'get', side_effect=[RuntimeError('storage unavailable'), read(OwnerBoundary.create(41))]) as lookup:
        unavailable = _flow.get_all_oauth_status(owner_user_id=41)
        recovered = _flow.get_all_oauth_status(owner_user_id=41)
    assert all(not status['provider_ready'] for status in unavailable.values())
    assert all(status['provider_ready'] for status in recovered.values())
    assert lookup.call_count == 2


def test_group_projection_preserves_tenant_boundary_and_live_token_health(routing):
    for provider in outbound.OAUTH_PROVIDERS:
        outbound.provision_oauth_provider(provider, owner_user_id=41, tenant_id='tenant-a', repository=routing)
    own = outbound.managed_oauth_provider_statuses(owner_user_id=41, tenant_id='tenant-a', repository=routing)
    foreign = outbound.managed_oauth_provider_statuses(owner_user_id=41, tenant_id='tenant-b', repository=routing)
    assert all(status['provider_ready'] for status in own.values())
    assert all(not status['provider_ready'] for status in foreign.values())
    with mock.patch('lib.oauth.token_store.load_token', return_value=None) as tokens:
        logged_out = _flow.get_all_oauth_status(owner_user_id=41)
    assert all(not status['authenticated'] for status in logged_out.values())
    assert all(status['provider_ready'] for status in logged_out.values())
    assert tokens.call_args_list == [
        mock.call(provider) for provider in outbound.OAUTH_PROVIDERS]


def test_unknown_provider_does_not_read_an_owner_snapshot(routing):
    with mock.patch.object(routing, 'get', wraps=routing.get) as lookup:
        status = outbound.managed_oauth_provider_status('unknown', owner_user_id=41)
    assert status == {'provider_ready': False, 'provider_id': '', 'model_ids': []}
    lookup.assert_not_called()

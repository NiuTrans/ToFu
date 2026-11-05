"""One OAuth status response resolves each credential once and reads routing once.

A shared Codex login revalidates native metadata with dozens of filesystem
probes per resolution, and every probe releases the GIL; repeating it for the
catalog fingerprint, the card and the reset offer multiplied response latency
on a loaded server. These specifications count the resolutions and authority
reads a response performs, and prove that no snapshot outlives its response.
"""
import asyncio
import base64
import hashlib
import json
import os
import stat
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from lib.model_routing import InMemoryModelRoutingRepository, normalize_document
from lib.model_routing.managed_provider import remove_provider_resources_in_place
from lib.oauth import codex_catalog, codex_shared_login as shared, codex_usage, outbound
from lib.oauth import token_store
from lib.oauth.manager import _flow

pytestmark = pytest.mark.unit

ACCOUNT = 'account-status'


def _jwt(claims):
    return 'header.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=') + '.signature'


def _auth(expire):
    claims = {'exp': expire, 'https://api.openai.com/auth': {
        'chatgpt_account_id': ACCOUNT, 'chatgpt_plan_type': 'pro'}}
    return {'auth_mode': 'chatgpt', 'tokens': {
        'access_token': _jwt(claims), 'refresh_token': 'NEVER-COPY-THIS',
        'id_token': _jwt({'email': 'status@example.test'}), 'account_id': ACCOUNT}}


def _write_private(path, payload):
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(token_store, '_config_path', lambda p: str(tmp_path / 'tofu' / p))
    return tmp_path / 'tofu' / 'oauth'


@pytest.fixture
def status(tmp_path, monkeypatch, store):
    """A personal deployment borrowing a local Codex login, behind the real route."""
    monkeypatch.setattr('lib.oauth.codex_command_login.command_context',
                        lambda **kwargs: {'shared_login_issue': 'codex_command_missing'})
    home = tmp_path / 'codex'
    home.mkdir()
    _write_private(home / 'auth.json', _auth(time.time() + 3600))
    monkeypatch.setenv('CODEX_HOME', str(home))
    monkeypatch.setenv('TOFU_CODEX_SHARED_LOGIN', '1')
    monkeypatch.setattr('runtime_guards.load_deployment_configuration',
                        lambda: SimpleNamespace(mode='personal'))
    monkeypatch.setattr(shared, '_agents', lambda owner: [])
    shared._remote.clear()

    cache = store / 'codex_models_cache.json'
    monkeypatch.setattr(codex_catalog, '_cache_path', lambda: str(cache))
    monkeypatch.setattr(codex_catalog, 'codex_client_version', lambda: '0.0.0')
    monkeypatch.setattr(codex_usage, '_cache_path', lambda: str(store / 'codex_usage_reset_cache.json'))
    monkeypatch.setattr(codex_usage, 'trigger_codex_usage_reset_refresh', lambda **kwargs: False)
    codex_usage._reset_codex_usage_state_for_tests()
    monkeypatch.setattr('lib.desktop.egress.egress_status', lambda host, user_id='': {'state': 'direct'})
    monkeypatch.setattr(outbound, '_activate_oauth_config_change', lambda: None)
    monkeypatch.setattr(_flow, '_active_flows', {})
    repository = InMemoryModelRoutingRepository()
    monkeypatch.setattr('lib.model_routing.ModelRoutingRepository', lambda: repository)

    shared.reconcile_shared_login(owner_user_id=1)
    assert token_store.load_stored_token('codex')['codex_shared_reference']['account_id'] == ACCOUNT
    cache.write_text(json.dumps({
        'schema_version': 1, 'fetched_at': '2026-10-30T00:00:00Z', 'fetched_at_unix': time.time(),
        'etag': 'e1', 'client_version': '0.0.0',
        'account_fingerprint': hashlib.sha256(ACCOUNT.encode()).hexdigest()[:16],
        'models': [{'slug': 'gpt-5.5', 'display_name': 'GPT-5.5', 'visibility': 'list',
                    'input_modalities': ['text', 'image'], 'context_window': 272000}],
    }))
    outbound.reconcile_oauth_providers(owner_user_id=1)

    resolutions = []
    real_resolve = shared.resolve_reference

    def counting_resolve(reference, *, refresh=False):
        resolutions.append('refresh' if refresh else 'passive')
        return real_resolve(reference, refresh=refresh)

    monkeypatch.setattr(shared, 'resolve_reference', counting_resolve)

    from quart import Quart, g
    import routes.api_v1.oauth as oauth_route

    app = Quart(__name__)
    app.register_blueprint(oauth_route.api_v1_oauth_bp)

    @app.before_request
    async def _principal():
        from lib.api_keys._context import local_admin_context
        from routes.api_v1.auth import _stamp_principal
        g.auth_ctx = local_admin_context()
        _stamp_principal()

    def get(*, reconcile_due):
        monkeypatch.setitem(oauth_route._oauth_reconcile_state, 'at',
                            0.0 if reconcile_due else time.monotonic())
        resolutions.clear()

        async def go():
            response = await app.test_client().get('/api/v1/oauth/status')
            return response.status_code, await response.get_json()
        code, body = asyncio.run(go())
        assert code == 200, body
        return body

    yield SimpleNamespace(get=get, resolutions=resolutions, repository=repository,
                          home=home, boundary=outbound._oauth_owner_boundary(1, None))
    shared._remote.clear()
    codex_usage._reset_codex_usage_state_for_tests()


def _cards(body):
    return {provider: {k: v for k, v in card.items() if k != 'reset_offer'}
            for provider, card in body.items() if isinstance(card, dict)}


def test_steady_response_resolves_the_shared_login_once_and_reads_routing_once(status):
    with mock.patch.object(status.repository, 'get', wraps=status.repository.get) as read:
        body = status.get(reconcile_due=False)
    assert body['codex']['authenticated']
    assert body['codex']['model_ids'] == ['gpt-5.5']
    assert body['codex']['catalog_model_count'] == 1
    assert body['codex']['reset_offer']['state'] == 'unknown'
    # Catalog fingerprint, card and reset offer share one passive resolution.
    assert status.resolutions == ['passive']
    assert read.call_count == 1


def test_reconcile_due_response_refreshes_once_and_reuses_its_readback(status):
    steady = status.get(reconcile_due=False)
    with mock.patch.object(status.repository, 'get', wraps=status.repository.get) as read:
        reconciled = status.get(reconcile_due=True)
    # The refreshing read may renew the source, so one passive read follows it.
    assert status.resolutions == ['refresh', 'passive']
    # Provisioning, deprovisioning and the cards observe one unchanged revision.
    assert read.call_count == 1
    assert _cards(reconciled) == _cards(steady)


def test_reconcile_that_commits_projects_the_committed_revision(status):
    current = status.repository.get(status.boundary)
    candidate = json.loads(json.dumps(current.document))
    remove_provider_resources_in_place(candidate, 'oauth_codex')
    candidate['revision'] = current.revision
    status.repository.compare_and_swap(
        status.boundary, normalize_document(candidate), expected_revision=current.revision)
    assert status.get(reconcile_due=False)['codex']['model_ids'] == []
    repaired = status.get(reconcile_due=True)
    # The pass re-provisioned the supply; its cards come from that commit.
    assert repaired['codex']['model_ids'] == ['gpt-5.5']
    assert _cards(repaired) == _cards(status.get(reconcile_due=False))


def test_a_snapshot_never_outlives_its_response(status):
    first = status.get(reconcile_due=False)
    renewed = time.time() + 7200
    replacement = status.home / 'auth.next'
    _write_private(replacement, _auth(renewed))
    os.replace(replacement, status.home / 'auth.json')
    second = status.get(reconcile_due=False)
    assert first['codex']['expire'] != second['codex']['expire']
    assert int(second['codex']['expire']) == int(renewed)
    assert status.resolutions == ['passive']


def test_refreshing_shared_read_invalidates_the_codex_snapshot(status):
    reference = token_store.load_stored_token('codex')['codex_shared_reference']
    with token_store.token_snapshot():
        token_store.load_token('codex')
        token_store.load_token('codex')
        shared.resolve_reference(reference, refresh=True)
        token_store.load_token('codex')
    assert status.resolutions == ['passive', 'refresh', 'passive']


def test_token_snapshot_resolves_each_provider_once_and_writes_invalidate(store):
    assert token_store.save_token('claude', {'access_token': 'first'})
    with mock.patch.object(token_store, 'load_stored_token',
                           wraps=token_store.load_stored_token) as reads:
        assert [token_store.load_token('claude')['access_token'] for _ in range(3)] == ['first'] * 3
        assert reads.call_count == 3  # Outside a snapshot every read is fresh.
        reads.reset_mock()
        with token_store.token_snapshot():
            first = token_store.load_token('claude')
            first['access_token'] = 'caller-mutation'
            with token_store.token_snapshot():  # Nested blocks share one snapshot.
                assert token_store.load_token('claude')['access_token'] == 'first'
            assert reads.call_count == 1
            assert token_store.save_token('claude', {'access_token': 'second'})
            assert token_store.load_token('claude')['access_token'] == 'second'
            assert token_store.delete_token('claude')
            assert token_store.load_token('claude') is None
            assert reads.call_count == 3
        assert token_store.load_token('claude') is None
        assert reads.call_count == 4


def test_private_store_reads_without_metadata_writes(store, monkeypatch):
    assert token_store.save_token('claude', {'access_token': 'private'})
    writes = []
    monkeypatch.setattr(os, 'chmod', lambda *args, **kwargs: writes.append(('chmod', args)))
    monkeypatch.setattr(os, 'mkdir', lambda *args, **kwargs: writes.append(('mkdir', args)))
    assert token_store.load_stored_token('claude')['access_token'] == 'private'
    assert writes == []


def test_legacy_store_modes_are_tightened_on_read(store):
    store.mkdir(parents=True)
    path = store / 'claude.json'
    path.write_text(json.dumps({'access_token': 'legacy'}))
    path.chmod(0o644)
    store.chmod(0o755)
    assert token_store.load_stored_token('claude')['access_token'] == 'legacy'
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.stat().st_mode) == 0o700

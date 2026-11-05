"""Builtin service faults are redacted 500s; only typed client faults answer 4xx.

Each route below used to answer any builtin ``ValueError``/``TypeError``/
``KeyError``/``RuntimeError`` from its service call as a client fault, echoing
the raw text. A programming, configuration or durable-data fault must reach
the redacted 500 boundary instead, while genuine caller mistakes keep their
400 and message. Service seams are patched at the route boundary on a bare
application with the production error handlers, so no storage is touched.
"""

from __future__ import annotations

import asyncio
import io

import pytest
from werkzeug.datastructures import FileStorage

from lib.error_envelope import BadRequest

pytestmark = pytest.mark.api

PRIVATE = 'private-fault-detail'


def _raise(error):
    def fail(*args, **kwargs):
        raise error
    return fail


def _raise_async(error):
    async def fail(*args, **kwargs):
        raise error
    return fail


def _app(blueprint):
    from quart import g

    from lib.api_keys import local_admin_context
    from lib.app.app_factory import create_base_app
    from lib.http_transport.error_handlers import register_http_error_handlers

    app = create_base_app(__name__, {'TESTING': True})
    register_http_error_handlers(app)

    @app.before_request
    async def _grant():
        g.auth_ctx = local_admin_context()
        g.rate_decision = None

    app.register_blueprint(blueprint)
    return app


def _call(blueprint, method, path, **kwargs):
    async def go():
        client = _app(blueprint).test_client()
        response = await getattr(client, method)(path, **kwargs)
        return response.status_code, await response.get_data(as_text=True)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(go())
    finally:
        loop.close()


def _assert_redacted_500(result):
    status, body = result
    assert status == 500
    assert PRIVATE not in body


# ── credentials ──────────────────────────────────────────────────────

def _credentials():
    from routes.api_v1.credentials import api_v1_credentials_bp
    return api_v1_credentials_bp


def test_credential_reveal_internal_fault_is_redacted_500(monkeypatch):
    monkeypatch.setattr('lib.credentials_vault.normalize_name',
                        _raise(ValueError(PRIVATE)))
    _assert_redacted_500(_call(
        _credentials(), 'post', '/api/v1/credentials/github_token/reveal'))


def test_credential_reveal_invalid_name_keeps_400():
    status, body = _call(_credentials(), 'post',
                         '/api/v1/credentials/Bad%20Name!/reveal')
    assert status == 400 and 'is not a valid credential name' in body


# ── API keys ─────────────────────────────────────────────────────────

def _keys(monkeypatch):
    import routes.api_v1.keys as keys
    monkeypatch.setattr(keys, '_credential_boundary', lambda: (1, None))
    return keys


def test_key_create_internal_fault_is_redacted_500(monkeypatch):
    keys = _keys(monkeypatch)
    monkeypatch.setattr(keys, 'create_key', _raise(ValueError(PRIVATE)))
    _assert_redacted_500(_call(keys.api_v1_keys_bp, 'post', '/api/v1/keys',
                               json={'name': 'ci', 'scopes': ['tasks']}))


def test_key_create_non_numeric_expiry_keeps_400(monkeypatch):
    keys = _keys(monkeypatch)
    status, body = _call(keys.api_v1_keys_bp, 'post', '/api/v1/keys', json={
        'name': 'ci', 'scopes': ['tasks'], 'expires_at': 'soon'})
    assert status == 400
    assert 'credential limits and expiry must be numeric' in body


# ── agent run ────────────────────────────────────────────────────────

def _agent_run(monkeypatch):
    import routes.api_v1.agent_run as agent_run
    monkeypatch.setattr('routes.api_v1.auth.request_user_id', lambda: 1)
    return agent_run


def test_agent_run_message_parser_fault_is_redacted_500(monkeypatch):
    agent_run = _agent_run(monkeypatch)
    monkeypatch.setattr(agent_run, 'require_list', _raise(ValueError(PRIVATE)))
    _assert_redacted_500(_call(agent_run.api_v1_agent_run_bp, 'post',
                               '/api/v1/agent/run', json={'messages': []}))


def test_agent_run_non_list_messages_keep_400(monkeypatch):
    agent_run = _agent_run(monkeypatch)
    status, body = _call(agent_run.api_v1_agent_run_bp, 'post',
                         '/api/v1/agent/run', json={'messages': 'hello'})
    assert status == 400 and 'messages must be a list' in body


# ── subscription adapter ─────────────────────────────────────────────

def _adapter(monkeypatch, failure):
    import routes.api_v1.adapter as adapter
    monkeypatch.setattr(adapter, '_caller_uid', lambda: '1')
    monkeypatch.setattr(adapter, '_known_agent',
                        lambda agent_id, uid: {'agent_id': agent_id, 'name': 'Agent'})
    monkeypatch.setattr('lib.desktop.adapter.delete_adapter_account', _raise(failure))
    return _call(adapter.api_v1_adapter_bp, 'delete', '/api/v1/adapter/accounts',
                 json={'agent_id': 'agent-1', 'name': 'account'})


def test_adapter_account_delete_internal_fault_is_redacted_500(monkeypatch):
    _assert_redacted_500(_adapter(monkeypatch, ValueError(PRIVATE)))


def test_adapter_account_delete_unknown_account_keeps_400(monkeypatch):
    status, body = _adapter(
        monkeypatch, BadRequest('unknown adapter account', field='name'))
    assert status == 400 and 'unknown adapter account' in body


def test_adapter_unknown_account_is_typed_at_its_owner(monkeypatch):
    from lib.desktop import adapter

    monkeypatch.setattr(adapter, 'adapter_accounts', lambda *a, **kw: [])
    with pytest.raises(BadRequest) as caught:
        adapter.delete_adapter_account('agent-1', 'account', user_id=1)
    assert caught.value.field == 'name'


# ── media attachments ────────────────────────────────────────────────

def _media(monkeypatch, failure):
    import routes.api_v1.media as media
    monkeypatch.setattr(media, 'request_user_id', lambda: 1)
    monkeypatch.setattr('lib.knowledge.html_application.application_html_projector',
                        lambda app: None)
    monkeypatch.setattr('lib.media_attachments.ingest_document', _raise(failure))
    upload = {'file': FileStorage(stream=io.BytesIO(b'evidence'), filename='a.txt')}
    return _call(media.api_v1_media_bp, 'post', '/api/v1/media/attachments',
                 files=upload)


def test_media_ingest_internal_fault_is_redacted_500(monkeypatch):
    _assert_redacted_500(_media(monkeypatch, ValueError(PRIVATE)))


def test_media_ingest_storage_outage_keeps_storage_taxonomy(monkeypatch):
    from lib.storage.errors import StorageError

    status, body = _media(monkeypatch, StorageError(
        'database_unavailable', PRIVATE, retryable=True))
    assert status == 503 and 'database_unavailable' in body


def test_media_ingest_rejected_document_keeps_400(monkeypatch):
    from lib.knowledge.ingest import KnowledgeIngestError

    status, body = _media(monkeypatch, KnowledgeIngestError('Unsupported or binary file type: .bin'))
    assert status == 400 and 'Unsupported or binary file type' in body


# ── billing ──────────────────────────────────────────────────────────

def _billing(monkeypatch):
    import routes.api_v1.billing as billing
    monkeypatch.setattr(billing, '_billing_disabled_response', lambda: None)
    monkeypatch.setattr(billing, '_resolve_target_user', lambda: 'usr_local')
    return billing


def test_billing_ledger_non_integer_page_keeps_400(monkeypatch):
    billing = _billing(monkeypatch)
    status, body = _call(billing.api_v1_billing_bp, 'get',
                         '/api/v1/billing/ledger?limit=many')
    assert status == 400 and 'limit/offset must be integers' in body


@pytest.mark.parametrize(('provider', 'entry', 'failure'), [
    ('alipay', 'create_alipay_order', _raise(ValueError(PRIVATE))),
    ('alipay', 'create_alipay_order', _raise(RuntimeError(PRIVATE))),
    ('stripe', 'create_stripe_checkout', _raise_async(RuntimeError(PRIVATE))),
    ('stripe', 'create_stripe_checkout', _raise_async(ValueError(PRIVATE))),
])
def test_checkout_provider_fault_is_redacted_500(monkeypatch, provider, entry, failure):
    billing = _billing(monkeypatch)
    monkeypatch.setattr(f'lib.billing.payments.{entry}', failure)
    _assert_redacted_500(_call(billing.api_v1_billing_bp, 'post', '/api/v1/billing/checkout',
                               json={'provider': provider, 'amount_minor': 999}))


@pytest.mark.parametrize(('provider', 'entry', 'failure', 'kind'), [
    ('alipay', 'create_alipay_order',
     _raise(BadRequest('Alipay not configured', field='provider')), 'not_configured'),
    ('stripe', 'create_stripe_checkout',
     _raise_async(BadRequest('Stripe rejected checkout session: amount too small')),
     'stripe_checkout_failed'),
])
def test_checkout_typed_client_fault_keeps_400(monkeypatch, provider, entry, failure, kind):
    billing = _billing(monkeypatch)
    monkeypatch.setattr(f'lib.billing.payments.{entry}', failure)
    status, body = _call(billing.api_v1_billing_bp, 'post', '/api/v1/billing/checkout',
                         json={'provider': provider, 'amount_minor': 999})
    assert status == 400 and kind in body


# ── evolution ────────────────────────────────────────────────────────

def _evolution(monkeypatch):
    import routes.api_v1.evolution as evolution
    monkeypatch.setattr(evolution, 'request_user_id', lambda: 1)
    return evolution


@pytest.mark.parametrize('failure', [ValueError(PRIVATE), TypeError(PRIVATE)])
def test_evolution_library_builtin_fault_is_redacted_500(monkeypatch, failure):
    evolution = _evolution(monkeypatch)
    monkeypatch.setattr('lib.evolution.programs.get_program', _raise(failure))
    _assert_redacted_500(_call(evolution.api_v1_evolution_bp, 'get',
                               '/api/v4/evolution/programs/prog-1'))


def test_evolution_contract_fault_keeps_400(monkeypatch):
    from lib.evolution.contracts import EvolutionContractError

    evolution = _evolution(monkeypatch)
    monkeypatch.setattr('lib.evolution.programs.get_program',
                        _raise(EvolutionContractError('invalid evolution program id')))
    status, body = _call(evolution.api_v1_evolution_bp, 'get',
                         '/api/v4/evolution/programs/prog-1')
    assert status == 400 and 'invalid evolution program id' in body


def test_evolution_caller_id_is_a_contract_fault():
    from lib.evolution import records
    from lib.evolution.contracts import EvolutionContractError

    with pytest.raises(EvolutionContractError):
        records.entity_key('not a valid id', 1, kind='program')


@pytest.mark.parametrize(('path', 'message'), [
    ('/api/v4/evolution/programs?limit=many', 'invalid literal for int()'),
    ('/api/v4/evolution/runs/run-1/coordination?after=many', 'cursor must be an integer'),
])
def test_evolution_query_parse_keeps_400(monkeypatch, path, message):
    evolution = _evolution(monkeypatch)
    status, body = _call(evolution.api_v1_evolution_bp, 'get', path)
    assert status == 400 and message in body


# ── database panel ───────────────────────────────────────────────────

def _database_panel(monkeypatch):
    from lib.database_panel import probe
    import routes.api_v1.database_panel as panel

    monkeypatch.setattr(probe, 'deployment_mode', lambda: 'personal')
    monkeypatch.setattr(probe, 'engine_status', lambda runtime=None: {'backend': 'tofudb'})
    monkeypatch.setattr(probe, 'fastpath_state', lambda backend, runtime=None: {'supported': True})
    return probe, panel


_PANEL_CASES = [
    ('configure_search_projection_directory', {'searchProjection': {'directory': '/srv/cache'}}),
    ('configure_native_fastpath', {'fastPath': {'enabled': True, 'directory': '/srv/fast'}}),
]


@pytest.mark.parametrize(('entry', 'body'), _PANEL_CASES)
def test_database_paths_internal_fault_is_redacted_500(monkeypatch, entry, body):
    probe, panel = _database_panel(monkeypatch)
    monkeypatch.setattr(probe, entry, _raise(ValueError(PRIVATE)))
    _assert_redacted_500(_call(panel.api_v1_database_panel_bp, 'post',
                               '/api/v1/database/paths', json=body))


@pytest.mark.parametrize(('entry', 'body'), _PANEL_CASES)
def test_database_paths_typed_rejection_keeps_400(monkeypatch, entry, body):
    probe, panel = _database_panel(monkeypatch)
    monkeypatch.setattr(probe, entry, _raise(BadRequest('directory must be writable')))
    status, response = _call(panel.api_v1_database_panel_bp, 'post',
                             '/api/v1/database/paths', json=body)
    assert status == 400 and 'directory must be writable' in response


def test_search_projection_caller_directory_is_typed_at_its_owner(tmp_path):
    from lib.storage_authority_sql import projection_paths

    with pytest.raises(BadRequest, match='absolute path') as caught:
        projection_paths.save_search_projection_directory(
            tmp_path / 'data', 'relative/cache', environ={})
    assert caught.value.field == 'searchProjection.directory'
    assert not (tmp_path / 'data').exists()

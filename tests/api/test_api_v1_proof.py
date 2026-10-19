"""Proof API surface: auth boundary, owner fencing, export and verify.

Full-stack through the real assembler/verifier with the four evidence
readers faked at their source modules (``lib.tasks_pkg.*``), so the route,
scope gate, and owner semantics are what these tests pin.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

pytest_plugins = ('tests._credential_sidecar',)

pytestmark = pytest.mark.unit

OWNER = 12_021
TASK_ID = 'task-proof-1'


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture
def client(monkeypatch):
    from quart import Quart
    app = Quart(__name__, static_folder=None)
    app.config.setdefault('PROVIDE_AUTOMATIC_OPTIONS', True)
    app.config['TESTING'] = True

    from routes.api_v1.auth import (
        attach_rate_headers, bearer_auth_before_request,
    )
    app.before_request(bearer_auth_before_request)
    app.after_request(attach_rate_headers)

    # Production's exception boundary: require_*'s BadRequest becomes the
    # canonical 400 envelope (lib/http_error_handlers).
    from lib.http_transport.error_handlers import register_http_error_handlers
    register_http_error_handlers(app)

    from routes.api_v1.proof import api_v1_proof_bp
    app.register_blueprint(api_v1_proof_bp)

    snapshot = SimpleNamespace(
        task_id=TASK_ID, user_id=OWNER, status='done', conv_id='conv-1',
        content='deliverable text', thinking='', error=None,
        metadata={'finishReason': 'stop', 'model': 'kimi-k3',
                  'usage': {'input_tokens': 3},
                  'modifiedFiles': 0, 'modifiedFileList': []},
        created_at_ms=1, updated_at_ms=2, completed_at_ms=3,
        event_replay={'base_cursor': 0, 'next_cursor': 0},
        terminal_payload_loaded=True)
    state = {'snapshot': snapshot,
             'trace': {'version': 1, 'taskId': TASK_ID,
                       'eventsAvailable': True, 'spans': []}}
    monkeypatch.setattr(
        'lib.tasks_pkg.durable_chat_replay.load_durable_chat_task',
        lambda task_id, *, user_id: (
            state['snapshot']
            if user_id == OWNER and task_id == TASK_ID else None))
    monkeypatch.setattr(
        'lib.tasks_pkg.turn_trace.read_persisted_task_trace',
        lambda task_id, *, user_id: state['trace'])
    monkeypatch.setattr(
        'lib.tasks_pkg.request_inspector.fold_request_log',
        lambda task_id: {'taskId': task_id,
                         'requests': [{'roundNum': 1, 'turn': 'request:aa'}],
                         'eventsAvailable': True, 'requestCount': 1,
                         'coverage': 'full'})
    monkeypatch.setattr(
        'lib.tasks_pkg.request_inspector.get_request_payload',
        lambda task_id, round_num, turn='', kind='request', user_id=None: {
            'taskId': task_id, 'roundNum': round_num, 'turn': turn,
            'kind': 'request', 'model': 'kimi-k3',
            'messages': [{'role': 'user', 'content': 'hi'}], 'tools': []})

    from lib.api_keys import create_key
    _row, token = create_key(owner_user_id=OWNER, name='proof-test',
                             scopes=['tasks'])
    _row2, chat_token = create_key(owner_user_id=OWNER,
                                   name='proof-no-tasks', scopes=['chat'])
    return app.test_client(), token, chat_token, state


def _headers(token):
    return {'Authorization': f'Bearer {token}'}


def _export(cli, token, task_id=TASK_ID):
    return _run(cli.get(f'/api/v1/tasks/{task_id}/proof',
                        headers=_headers(token)))


def _verify(cli, token, bundle):
    return _run(cli.post('/api/v1/proof/verify',
                         json={'bundle': bundle},
                         headers=_headers(token)))


def test_export_carries_explicit_owner_identity(client):
    """The assembly read is fenced by the RESOLVED principal, never by an
    ambient/global: this harness's tokenless request resolves to the
    synthetic open-mode principal, which is not OWNER, so the owned task
    stays invisible (404) instead of leaking through implicit identity."""
    cli, _token, _chat, _state = client
    resp = _run(cli.get(f'/api/v1/tasks/{TASK_ID}/proof'))
    assert resp.status_code == 404


def test_export_requires_tasks_scope(client):
    cli, _token, chat_token, _state = client
    resp = _export(cli, chat_token)
    assert resp.status_code == 403


def test_export_happy_path(client):
    cli, token, _chat, _state = client
    resp = _export(cli, token)
    assert resp.status_code == 200
    body = _run(resp.get_json())
    assert body['ok'] is True
    proof = body['proof']
    assert proof['coverage'] == 'full'
    assert proof['owner'] == OWNER
    assert proof['subject']['identity']['taskId'] == TASK_ID
    assert [i['kind'] for i in proof['evidence']] == [
        'turn_trace', 'request_round', 'file_receipt']
    # No message or deliverable plaintext rides the bundle.
    assert 'deliverable text' not in str(proof)
    assert 'hi' not in str(proof['evidence'])


def test_export_unknown_task_is_404(client):
    cli, token, _chat, _state = client
    resp = _export(cli, token, task_id='no-such-task')
    assert resp.status_code == 404


def test_verify_exported_bundle_is_valid_at_full_scope(client):
    cli, token, _chat, _state = client
    proof = _run(_export(cli, token).get_json())['proof']
    verification = _run(_verify(cli, token, proof).get_json())['verification']
    assert verification['verificationScope'] == 'full'
    assert verification['verdict'] == 'valid'
    assert verification['checks']['evidence']['matched'] == 3


def test_verify_tampered_bundle(client):
    cli, token, _chat, _state = client
    proof = _run(_export(cli, token).get_json())['proof']
    proof['verdicts']['task']['status'] = 'error'  # stale content address
    verification = _run(_verify(cli, token, proof).get_json())['verification']
    assert verification['verdict'] == 'tampered'


def test_verify_foreign_bundle_stays_structural(client):
    cli, token, _chat, _state = client
    proof = _run(_export(cli, token).get_json())['proof']
    proof['owner'] = OWNER + 1  # another owner's bundle: no server re-read
    from lib.proof.digests import bundle_id
    proof['bundleId'] = bundle_id(
        {k: v for k, v in proof.items() if k != 'bundleId'})
    verification = _run(_verify(cli, token, proof).get_json())['verification']
    assert verification['verificationScope'] == 'structural'
    assert verification['verdict'] == 'valid'


def test_verify_rejects_unknown_body_fields(client):
    cli, token, _chat, _state = client
    resp = _run(cli.post('/api/v1/proof/verify',
                         json={'bundle': {}, 'extra': 1},
                         headers=_headers(token)))
    assert resp.status_code == 400


def test_verify_rejects_non_dict_bundle(client):
    cli, token, _chat, _state = client
    resp = _run(cli.post('/api/v1/proof/verify', json={'bundle': 'nope'},
                         headers=_headers(token)))
    assert resp.status_code == 400


def test_verify_oversize_bundle_is_413(client):
    cli, token, _chat, _state = client
    proof = _run(_export(cli, token).get_json())['proof']
    # `usage` is a free-form object, so it can carry the raw-byte pad that
    # pushes the bundle past the 1 MiB cap the route enforces pre-verify.
    proof['verdicts']['task']['usage'] = {'pad': 'x' * 1_100_000}
    resp = _verify(cli, token, proof)
    assert resp.status_code == 413

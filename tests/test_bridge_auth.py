"""Device bridges accept only owner-scoped credentials or process capability."""

from __future__ import annotations

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('open')]

PUBLIC_PEER = {'client': ('203.0.113.7', 5555)}


def _poll_frame(path: str) -> dict:
    if path == '/api/browser/poll':
        return {
            'clientId': 'bridge-auth-browser',
            'protocolVersion': 2,
            'capabilities': [],
            'results': [],
        }
    return {
        'agent': {'agent_id': 'bridge-auth-desktop'},
        'results': [],
        'streams': [],
    }


def _token(*, owner_user_id=41, scopes=('agents:bridge',)) -> str:
    from lib.api_keys import create_key

    _row, token = create_key(
        owner_user_id=owner_user_id,
        name=f'bridge-owner-{owner_user_id}',
        scopes=list(scopes),
    )
    return token


@pytest.mark.parametrize('path,method', [
    ('/api/browser/poll', 'POST'),
    ('/api/browser/file-transfers/deadbeef/start', 'POST'),
    ('/api/browser/commands', 'GET'),
    ('/api/browser/result', 'POST'),
    ('/api/desktop/poll', 'POST'),
])
def test_missing_and_unknown_credentials_fail_closed(
    flask_client, path, method,
):
    missing = flask_client.open(
        path, method=method, json={}, scope_base=PUBLIC_PEER)
    unknown = flask_client.open(
        path,
        method=method,
        json={},
        headers={'X-Bridge-Secret': 'not-a-credential'},
        scope_base=PUBLIC_PEER,
    )
    assert missing.status_code == 401
    assert unknown.status_code == 401


@pytest.mark.parametrize('path', ['/api/browser/poll', '/api/desktop/poll'])
def test_owner_scoped_bridge_credential_is_address_independent(
    flask_client, path, monkeypatch,
):
    async def no_browser_commands(**_kwargs):
        return []

    async def no_desktop_commands(**_kwargs):
        return []

    import lib.browser.queue as browser_queue
    import lib.desktop
    monkeypatch.setattr(
        browser_queue, 'wait_for_commands_async', no_browser_commands)
    monkeypatch.setattr(
        lib.desktop, 'take_pending_commands_async', no_desktop_commands)
    response = flask_client.post(
        path,
        json=_poll_frame(path),
        headers={'X-Bridge-Secret': _token()},
        scope_base=PUBLIC_PEER,
    )
    assert response.status_code == 200


def test_non_bridge_credential_is_rejected(flask_client):
    response = flask_client.post(
        '/api/desktop/poll',
        json={},
        headers={'X-Bridge-Secret': _token(scopes=('chat',))},
    )
    assert response.status_code == 401


def test_process_capability_is_desktop_only(flask_client, monkeypatch):
    from lib.bridge_auth import process_agent_token

    async def no_desktop_commands(**_kwargs):
        return []

    import lib.desktop
    monkeypatch.setattr(
        lib.desktop, 'take_pending_commands_async', no_desktop_commands)

    headers = {'X-Bridge-Secret': process_agent_token()}
    desktop = flask_client.post(
        '/api/desktop/poll', json=_poll_frame('/api/desktop/poll'),
        headers=headers,
    )
    browser = flask_client.post('/api/browser/poll', json={}, headers=headers)
    assert desktop.status_code == 200
    assert browser.status_code == 401


def test_preflight_never_authenticates_or_mutates(flask_client):
    response = flask_client.open('/api/browser/poll', method='OPTIONS')
    assert response.status_code == 204
    transfer = flask_client.open(
        '/api/browser/file-transfers/deadbeef/start', method='OPTIONS')
    assert transfer.status_code == 204


def test_browser_file_transfer_http_boundary_is_owner_device_scoped(
        flask_client):
    """Exercise metadata, raw chunk and completion through real bridge auth."""
    import hashlib
    import os

    from lib.browser.file_transfer import file_transfer_store

    file_transfer_store.clear_for_tests()
    created = file_transfer_store.create(
        owner_user_id='41', client_id='browser-a', profile='Work',
        source_url='https://x.test/download?version=latest', max_bytes=1024,
    )
    transfer_id = created['transferId']
    base = f'/api/browser/file-transfers/{transfer_id}'
    transfer_headers = {
        'X-Bridge-Secret': _token(owner_user_id=41),
        'X-Browser-Client-Id': 'browser-a',
        'X-Transfer-Token': created['transferToken'],
    }
    wrong_owner_headers = dict(
        transfer_headers,
        **{'X-Bridge-Secret': _token(owner_user_id=42)},
    )
    metadata = {
        'finalUrl': 'https://cdn.x.test/file.zip',
        'responseStatus': 200,
        'contentType': 'application/zip',
        'contentDisposition': 'attachment; filename="file.zip"',
        'contentLength': 8,
        'suggestedFilename': 'file.zip',
    }
    wrong_owner = flask_client.post(
        f'{base}/start', json=metadata, headers=wrong_owner_headers)
    assert wrong_owner.status_code == 403
    assert wrong_owner.get_json()['code'] == 'browser_file_transfer_forbidden'

    started = flask_client.post(
        f'{base}/start', json=metadata, headers=transfer_headers)
    assert started.status_code == 200
    payload = b'PK\x03\x04test'
    chunk_headers = dict(
        transfer_headers,
        **{
            'Content-Type': 'application/octet-stream',
            'X-Chunk-SHA256': hashlib.sha256(payload).hexdigest(),
        },
    )
    chunk = flask_client.open(
        f'{base}/chunks/0', method='PUT', data=payload,
        headers=chunk_headers,
    )
    assert chunk.status_code == 200
    assert chunk.get_json()['receivedBytes'] == len(payload)
    completed = flask_client.post(
        f'{base}/complete',
        json={'totalBytes': len(payload), 'chunkCount': 1},
        headers=transfer_headers,
    )
    assert completed.status_code == 200
    public = completed.get_json()
    assert public['location'] == 'server_staging'
    assert 'path' not in public

    receipt = file_transfer_store.consume_completed(
        transfer_id, owner_user_id='41', client_id='browser-a')
    try:
        with open(receipt['path'], 'rb') as stream:
            assert stream.read() == payload
    finally:
        try:
            os.unlink(receipt['path'])
        except FileNotFoundError:
            pass


def test_browser_file_transfer_control_envelopes_have_a_route_local_cap(
        flask_client):
    from lib.browser.file_transfer import file_transfer_store

    file_transfer_store.clear_for_tests()
    created = file_transfer_store.create(
        owner_user_id='41', client_id='browser-a', profile='Work',
        source_url='https://x.test/file.bin', max_bytes=1024,
    )
    headers = {
        'X-Bridge-Secret': _token(owner_user_id=41),
        'X-Browser-Client-Id': 'browser-a',
        'X-Transfer-Token': created['transferToken'],
        'Content-Type': 'application/json',
    }
    response = flask_client.post(
        f'/api/browser/file-transfers/{created["transferId"]}/start',
        data=b'{"padding":"' + (b'x' * (17 * 1024)) + b'"}',
        headers=headers,
    )
    assert response.status_code == 413
    assert response.get_json()['code'] == \
        'browser_file_transfer_control_too_large'
    assert file_transfer_store.abort(
        created['transferId'], owner_user_id='41', client_id='browser-a',
        internal=True,
    ) is True


@pytest.mark.parametrize('path', [
    '/api/v1/browser/status',
    '/api/v1/browser/clients',
    '/api/v1/desktop/status',
])
def test_operator_routes_do_not_use_the_device_gate(flask_client, path):
    response = flask_client.get(path)
    body = response.get_json(silent=True) or {}
    assert body.get('error') != 'bridge_auth_required'


@pytest.mark.parametrize('path', [
    '/api/v1/browser/status',
    '/api/v1/browser/clients',
])
def test_browser_status_never_enables_cross_origin_reads(flask_client, path):
    response = flask_client.get(
        path, headers={'Origin': 'https://evil.example.com'})
    assert 'Access-Control-Allow-Origin' not in response.headers
    assert 'Access-Control-Allow-Methods' not in response.headers
    assert 'Access-Control-Allow-Headers' not in response.headers

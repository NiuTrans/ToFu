"""HTTP contracts for browser access policy and adapter discovery."""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def browser_api(tmp_path, monkeypatch):
    from quart import Quart

    from lib.browser import access
    from lib.browser.queue import _state
    from routes.api_v1.auth import bearer_auth_before_request
    from routes.api_v1.browser import api_v1_browser_bp

    monkeypatch.setattr(access, '_STORE_PATH', str(tmp_path / 'browser_access.json'))
    monkeypatch.setenv('TUNNEL_TOKEN', 'browser-api-test-gate')
    with _state._clients_lock:
        _state._clients.clear()
    app = Quart(__name__)
    app.config['TESTING'] = True
    app.before_request(bearer_auth_before_request)
    app.register_blueprint(api_v1_browser_bp)
    yield app
    with _state._clients_lock:
        _state._clients.clear()


def test_access_and_adapter_endpoints_are_authenticated_and_user_scoped(
        browser_api):
    from lib.api_keys import create_key

    _row, token = create_key(name='browser-access-api', scopes=['chat'])
    headers = {'Authorization': f'Bearer {token}'}

    async def scenario():
        client = browser_api.test_client()
        response = await client.put(
            '/api/v1/browser/access', headers=headers,
            json={'read_denied_domains': ['https://WWW.Example.com/path']})
        assert response.status_code == 200
        body = await response.get_json()
        assert body['read_denied_domains'] == ['example.com']

        adapters = await client.get('/api/v1/browser/adapters', headers=headers)
        assert adapters.status_code == 200
        payload = await adapters.get_json()
        assert payload['count'] >= 2
        assert {row['id'] for row in payload['adapters']} >= {
            'xiaohongshu', 'modelplaza'}
        assert all(row['health']['status'] == 'offline'
                   for row in payload['adapters'])

        forged = await client.put(
            '/api/v1/browser/access', headers=headers,
            json={'write_grants': [{
                'domain': 'example.com', 'client_id': 'someone-elses-browser'}]})
        assert forged.status_code == 400

    _run(scenario())

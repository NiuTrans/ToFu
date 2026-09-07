"""HTTP contract for the agent-local CLIProxy OAuth lifecycle."""

from unittest import mock

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.auth_mode('open')]


def _agent():
    return {'agent_id': 'agent-1', 'name': 'laptop', 'online': True}


def test_adapter_oauth_start_route(flask_client, monkeypatch):
    import routes.api_v1.adapter as route
    monkeypatch.setattr(route, '_known_agent', lambda aid, uid: _agent())
    with mock.patch('lib.desktop.adapter.start_adapter_oauth', return_value={
            'provider': 'codex', 'status': 'started',
            'auth_url': 'https://auth.example/', 'state': 'safe_state'}):
        resp = flask_client.post('/api/v1/adapter/oauth/start', json={
            'agent_id': 'agent-1', 'provider': 'codex'})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['ok'] is True and body['state'] == 'safe_state'


def test_adapter_oauth_status_rejects_unsafe_state_before_relay(flask_client,
                                                               monkeypatch):
    import routes.api_v1.adapter as route
    monkeypatch.setattr(route, '_known_agent', lambda aid, uid: _agent())
    with mock.patch('lib.desktop.adapter.adapter_oauth_status') as target:
        resp = flask_client.get(
            '/api/v1/adapter/oauth/status?agent_id=agent-1&state=../bad')
    assert resp.status_code == 400
    target.assert_not_called()


def test_adapter_oauth_manual_callback_and_account_delete(flask_client,
                                                          monkeypatch):
    import routes.api_v1.adapter as route
    monkeypatch.setattr(route, '_known_agent', lambda aid, uid: _agent())
    with mock.patch('lib.desktop.adapter.submit_adapter_oauth_callback',
                    return_value={'status': 'ok'}) as callback:
        resp = flask_client.post('/api/v1/adapter/oauth/callback', json={
            'agent_id': 'agent-1', 'provider': 'claude',
            'state': 'safe_state',
            'redirect_url': 'http://localhost:54545/callback?code=x&state=safe_state',
        })
    assert resp.status_code == 200 and resp.get_json()['status'] == 'ok'
    callback.assert_called_once()

    with mock.patch('lib.desktop.adapter.delete_adapter_account',
                    return_value={'deleted': True, 'models': 0}) as delete:
        resp = flask_client.delete('/api/v1/adapter/accounts', json={
            'agent_id': 'agent-1', 'name': 'codex-user.json',
            'auth_index': 3,
        })
    assert resp.status_code == 200 and resp.get_json()['deleted'] is True
    assert delete.call_args.kwargs['auth_index'] == 3

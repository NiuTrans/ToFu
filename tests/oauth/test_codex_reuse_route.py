"""Explicit reuse keeps owner scoping and never starts a fresh OAuth flow."""
import asyncio
from unittest.mock import patch

import pytest
from quart import Quart
from routes.oauth import oauth_bp

pytestmark = pytest.mark.api


@pytest.mark.parametrize('result,expected', [
    (None, 'unavailable'),
    ({'status': 'success', 'credential_source': 'local_codex'}, 'success'),
    ({'status': 'error', 'error': 'Login unavailable'}, 'error'),
])
def test_explicit_reuse_never_falls_back(result, expected):
    app = Quart(__name__)
    app.register_blueprint(oauth_bp)

    async def run():
        with patch('routes.oauth._request_owner_user_id', return_value=7), \
             patch('lib.oauth.manager._flow.try_reuse_codex_login', return_value=result) as reuse, \
             patch('lib.oauth.manager.start_oauth_flow') as authorize, \
             patch('lib.oauth.codex_shared_login.local_login_issue', return_value='codex_access_expired') as diagnose:
            response = await app.test_client().post('/api/oauth/login', json={
                'provider': 'codex', 'reuse_only': True})
            body = await response.get_json()
            assert response.status_code == (400 if expected == 'error' else 200)
            if expected != 'error':
                assert body['status'] == expected
            if expected == 'unavailable':
                assert body['auth_issue'] == 'codex_access_expired'
                diagnose.assert_called_once_with(owner_user_id=7)
            else:
                diagnose.assert_not_called()
            reuse.assert_called_once_with(owner_user_id=7)
            authorize.assert_not_called()
    asyncio.run(run())


def test_reuse_rejects_other_provider():
    app = Quart(__name__)
    app.register_blueprint(oauth_bp)

    async def run():
        with patch('lib.oauth.manager._flow.try_reuse_codex_login') as reuse:
            response = await app.test_client().post('/api/oauth/login', json={
                'provider': 'claude', 'reuse_only': True})
            assert response.status_code == 400
            reuse.assert_not_called()
    asyncio.run(run())

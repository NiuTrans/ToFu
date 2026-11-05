"""Open mode never lends its synthetic administrator to another site's page.

Any web page the local user visits can send requests to a loopback Tofu.
Fetch Metadata (or, for older browsers, Origin) identifies those requests;
state-changing ones must authenticate like private mode instead of inheriting
the zero-setup local grant.
"""

from __future__ import annotations

import asyncio

import pytest
from quart import Quart

pytestmark = pytest.mark.unit


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setattr('routes.api_v1.auth._mode_requires_credential', lambda: False)
    monkeypatch.setattr('routes.api_v1.auth._OPEN_MODE_ALLOW_REMOTE', False)
    from routes.api_v1.auth import auth_before_request, require_scope

    application = Quart(__name__)
    application.before_request(auth_before_request)

    @application.route('/api/v4/fixture/admin',
                       methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
    @require_scope('admin')
    async def admin_route():
        return {'ok': True}

    return application


def _status(app, method, headers):
    async def run():
        async with app.test_client() as client:
            response = await client.open(
                '/api/v4/fixture/admin', method=method, headers=headers,
                data='{}')
            return response.status_code
    return asyncio.run(run())


@pytest.mark.parametrize('headers', [
    {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'},
    {'Sec-Fetch-Site': 'same-site', 'Origin': 'http://localhost:3000'},
    {'Origin': 'https://evil.example'},  # browser without Fetch Metadata
    {'Origin': 'null'},  # sandboxed frame or file:// page
])
@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
def test_cross_site_browser_write_gets_no_open_mode_grant(app, method, headers):
    assert _status(app, method, headers) == 401


@pytest.mark.parametrize('headers', [
    {'Sec-Fetch-Site': 'same-origin', 'Origin': 'http://localhost'},
    # A host-rewriting forwarded port keeps the browser's same-origin verdict.
    {'Sec-Fetch-Site': 'same-origin', 'Origin': 'https://ide.example'},
    {'Origin': 'http://localhost'},
    {},  # headless client
])
def test_same_origin_and_headless_writes_keep_open_mode_grant(app, headers):
    assert _status(app, 'POST', headers) == 200


def test_cross_site_safe_read_keeps_open_mode_grant(app):
    headers = {'Sec-Fetch-Site': 'cross-site', 'Origin': 'https://evil.example'}
    assert _status(app, 'GET', headers) == 200


@pytest.mark.parametrize('private,cross_site,peer_allowed', [
    (False, True, True),
    (False, False, False),
    (True, False, True),
])
def test_denied_request_validates_its_presented_credential_once(
    app, monkeypatch, private, cross_site, peer_allowed,
):
    from lib.api_keys import _validate
    from routes.api_v1 import auth

    calls = []
    class ReadAuthority:
        def query(self, operation, payload):
            calls.append(operation)
            assert payload['secret_hash']
            return None

    monkeypatch.setattr(auth, '_mode_requires_credential', lambda: private)
    monkeypatch.setattr(auth, 'open_mode_peer_allowed', lambda _peer: peer_allowed)
    monkeypatch.setattr(_validate, 'get_storage_client', lambda: ReadAuthority())
    monkeypatch.setattr(auth, 'validate_token', _validate.validate_token)
    headers = {'Authorization': 'Bearer tofu_live_rejected'}
    if cross_site:
        headers['Sec-Fetch-Site'] = 'cross-site'

    assert _status(app, 'POST', headers) == 401
    assert calls == ['credential.validate']


def test_next_request_revalidates_the_same_token_after_revocation(app, monkeypatch):
    from lib.api_keys import AuthContext
    from routes.api_v1 import auth

    calls = []
    allowed = AuthContext(key_id='key', owner_user_id=17,
                          scopes=frozenset({'admin'}))
    def validate(token):
        calls.append(token)
        return allowed if len(calls) == 1 else None

    monkeypatch.setattr(auth, 'open_mode_peer_allowed', lambda _peer: False)
    monkeypatch.setattr(auth, 'validate_token', validate)
    headers = {'Authorization': 'Bearer tofu_live_revoked'}

    assert _status(app, 'POST', headers) == 200
    assert _status(app, 'POST', headers) == 401
    assert calls == ['tofu_live_revoked', 'tofu_live_revoked']

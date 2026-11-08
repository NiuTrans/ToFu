"""Local-kernel boundary: default deny, one authority, updates owned by the launcher.

Every app and every web page on a phone can reach the kernel's loopback port.
The local kernel therefore runs the shared auth boundary in private mode,
answers only its own authority (``127.0.0.1:<port>``, against DNS rebinding),
and leaves code updates and restarts to the app that launched it.

The enumeration below assembles the production route graph with every view
replaced by a stub, so a stub answer means the boundary admitted the request.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = [pytest.mark.api, pytest.mark.auth_mode('private')]

_PORT = 43567
_AUTHORITY = f'127.0.0.1:{_PORT}'
_WRONG_SECRET = 'tofu_admin_' + 'f' * 64
_PROTECTED = '/api/v4/keys'

# The reviewed unauthenticated surface of the shared boundary: probes, API
# self-description, static assets, sign-in pages, and endpoints whose payload
# carries its own credential (signed webhooks). Route handlers may refuse
# further (``PUT /api/v4/auth/mode`` requires the admin scope). Device pairing
# is absent: devices dial the server they pair with, and nothing off the phone
# can dial a loopback kernel.
_PUBLIC = frozenset({
    'GET /api/docs', 'GET /api/health', 'GET /api/openapi.json',
    'GET /api/openapi.yaml', 'GET /api/ready', 'GET /api/redoc',
    'GET /api/v4/auth/mode', 'PUT /api/v4/auth/mode',
    'GET /api/v4/billing/pricing', 'PUT /api/v4/billing/pricing',
    'GET /api/v4/capabilities', 'GET /api/v4/keys/whoami', 'GET /api/v4/meta',
    'GET /api/v4/openapi.json', 'GET /api/v4/users/me',
    'POST /api/v4/users/login', 'POST /api/v4/users/logout',
    'POST /api/v4/users/signup',
    'POST /api/v4/billing/webhooks/alipay', 'POST /api/v4/billing/webhooks/stripe',
    'GET /dashboard', 'GET /dashboard/', 'GET /login', 'GET /login/',
    'GET /signup', 'GET /signup/',
    'GET /favicon.ico', 'GET /favicon.svg',
    'GET /health/live', 'GET /health/ready', 'GET /health/startup',
    'GET /static/<path:filename>',
})

_ARGUMENT = re.compile(
    r'<(?:(?P<converter>[a-zA-Z_][a-zA-Z0-9_]*)(?:\([^)]*\))?:)?[a-zA-Z_][a-zA-Z0-9_]*>')
_SAMPLES = {'int': '1', 'float': '1.5', 'path': 'a/b',
            'uuid': '00000000-0000-4000-8000-000000000000'}


def _local_kernel(app):
    app.extensions['tofu_local_kernel'] = {'host': _AUTHORITY}
    return app


@pytest.fixture(scope='module')
def kernel_app():
    import server
    return _local_kernel(server.create_app({'TESTING': True}))


@pytest.fixture(scope='module')
def stubbed_kernel_app():
    import server
    app = _local_kernel(server.create_app({'TESTING': True}))

    async def admitted(**_values):
        return '', 204

    for endpoint in list(app.view_functions):
        app.view_functions[endpoint] = admitted
    return app


@pytest.fixture(autouse=True)
def no_personal_credentials():
    from lib.api_keys import list_keys, revoke_key

    def revoke_all():
        for row in list_keys(owner_user_id=1):
            revoke_key(row['id'], owner_user_id=1)

    revoke_all()
    yield
    revoke_all()


@pytest.fixture
def secret():
    """A fresh install secret, admitted; revoked keys keep their unique hash."""
    import secrets

    from lib.api_keys import install_personal_key

    value = 'tofu_admin_' + secrets.token_hex(32)
    install_personal_key(value)
    return value


def _call(app, path, *, method='GET', headers=None):
    async def call():
        response = await app.test_client().open(
            path, method=method, headers={'Host': _AUTHORITY, **(headers or {})})
        return response.status_code, await response.get_data(as_text=True)
    return asyncio.run(call())


def _status(app, path, **kwargs):
    return _call(app, path, **kwargs)[0]


def test_every_route_outside_the_reviewed_public_surface_requires_a_credential(
    stubbed_kernel_app,
):
    websockets, statuses = [], {}

    async def probe():
        client = stubbed_kernel_app.test_client()
        for rule in stubbed_kernel_app.url_map.iter_rules():
            if getattr(rule, 'websocket', False):
                websockets.append(rule.rule)
                continue
            path = _ARGUMENT.sub(
                lambda match: _SAMPLES.get(match['converter'] or '', 'x'), rule.rule)
            for method in sorted(rule.methods - {'HEAD', 'OPTIONS'}):
                response = await client.open(
                    path, method=method, headers={'Host': _AUTHORITY})
                statuses[f'{method} {rule.rule}'] = response.status_code

    asyncio.run(probe())

    admitted = {route for route, status in statuses.items() if status != 401}
    assert admitted == _PUBLIC
    assert websockets == ['/api/push']


def test_the_install_secret_is_admitted_on_every_transport_the_boundary_accepts(
    stubbed_kernel_app, secret,
):
    transports = {
        'Authorization': f'Bearer {secret}',
        'x-api-key': secret,
        'Cookie': f'tofu_session={secret}',
    }
    for header, value in transports.items():
        assert _status(stubbed_kernel_app, _PROTECTED, headers={header: value}) == 204
        assert _status(stubbed_kernel_app, '/', headers={header: value}) == 204
    assert _status(stubbed_kernel_app, _PROTECTED) == 401
    assert _status(stubbed_kernel_app, '/') == 401
    for header, value in {
        'Authorization': f'Bearer {_WRONG_SECRET}',
        'x-api-key': _WRONG_SECRET,
        'Cookie': f'tofu_session={_WRONG_SECRET}',
    }.items():
        assert _status(stubbed_kernel_app, _PROTECTED, headers={header: value}) == 401

    # Health and meta are public probes; a presented but wrong secret is
    # still definitive at the boundary.
    for probe in ('/api/v4/meta', '/health/live', '/api/ready'):
        assert _status(stubbed_kernel_app, probe) == 204
        assert _status(stubbed_kernel_app, probe, headers={
            'Authorization': f'Bearer {_WRONG_SECRET}'}) == 401


@pytest.mark.parametrize('authority', [
    'evil.example:43567', 'localhost:43567', '127.0.0.1:1', '127.0.0.1',
])
def test_a_local_kernel_answers_only_its_own_authority(
    stubbed_kernel_app, secret, authority,
):
    for path in (_PROTECTED, '/', '/api/v4/meta', '/health/live', '/static/app.js'):
        assert _status(stubbed_kernel_app, path, headers={
            'Host': authority, 'Authorization': f'Bearer {secret}'}) == 403


class _UnfinishedBoot:
    def __init__(self, status):
        self.status = status

    def snapshot(self):
        return {'currentPhase': 3, 'phaseTotal': 8, 'failedPhase': 3,
                'error': 'storage refused', 'retryCount': 0}


@pytest.mark.parametrize('boot_status', ['running', 'failed'])
def test_a_foreign_authority_is_refused_before_any_other_gate_answers(
    stubbed_kernel_app, secret, monkeypatch, boot_status,
):
    monkeypatch.setitem(stubbed_kernel_app.extensions, 'tofu_boot_runner',
                        _UnfinishedBoot(boot_status))
    authorized = {'Authorization': f'Bearer {secret}'}
    for path in (_PROTECTED, '/api/v4/meta', '/api/v1/keys', '/'):
        assert _status(stubbed_kernel_app, path, headers={
            **authorized, 'Host': 'evil.example:43567'}) == 403, path
    assert _status(stubbed_kernel_app, _PROTECTED, headers=authorized) == 503


def test_the_push_socket_needs_the_secret_and_the_kernel_authority(kernel_app, secret):
    from quart.testing import WebsocketResponseError

    from routes.push import _resolve_push_ws_auth

    async def handshake(headers):
        # The test client reports a refused upgrade on the first receive.
        try:
            async with kernel_app.test_client().websocket(
                    '/api/push', headers=headers) as socket:
                await asyncio.wait_for(socket.receive(), timeout=10)
        except WebsocketResponseError as refused:
            return refused.response.status_code
        except TimeoutError:
            pass
        return 101

    assert asyncio.run(handshake({'Host': _AUTHORITY})) == 401
    assert asyncio.run(handshake({
        'Host': 'evil.example:43567', 'Cookie': f'tofu_session={secret}'})) == 403
    context, reason = _resolve_push_ws_auth(
        {'Host': _AUTHORITY}, {'tofu_session': secret}, ('127.0.0.1', 50000))
    assert context is not None and reason == 'token'


def test_device_pairing_is_refused_because_no_device_can_dial_a_local_kernel(
    kernel_app, secret,
):
    from lib.api_keys import list_keys

    assert _status(kernel_app, '/api/desktop/pair', method='POST') == 401
    authorized = {'Authorization': f'Bearer {secret}',
                  'Content-Type': 'application/json'}
    for path in ('/api/v4/desktop/pair-code', '/api/desktop/pair'):
        status, body = _call(kernel_app, path, method='POST', headers=authorized)
        assert status == 409, (path, body)
        assert 'loopback' in json.dumps(json.loads(body)).lower()
    assert [row for row in list_keys(owner_user_id=1)
            if 'agents:bridge' in (row.get('scopes') or ())] == []


def test_updates_and_restarts_belong_to_the_launching_app(
    kernel_app, secret, monkeypatch,
):
    import lib.self_update as self_update

    touched = []
    monkeypatch.setattr(self_update, 'observe_repository',
                        lambda *_a, **_k: touched.append('observe'))
    monkeypatch.setattr(self_update, 'check_for_update',
                        lambda *_a, **_k: touched.append('check'))
    monkeypatch.setattr(self_update, 'apply_update',
                        lambda *_a, **_k: touched.append('apply'))
    authorized = {'Authorization': f'Bearer {secret}',
                  'Content-Type': 'application/json'}

    for method, path in (('GET', '/api/v4/update/check'),
                         ('POST', '/api/v4/update/apply'),
                         ('POST', '/api/v4/update/restart')):
        status, body = _call(kernel_app, path, method=method, headers=authorized)
        assert status == 409, (path, body)
        assert 'launch' in json.dumps(json.loads(body)).lower()
    assert touched == []

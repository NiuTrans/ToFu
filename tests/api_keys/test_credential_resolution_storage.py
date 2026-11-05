"""Request-scoped selection observes current credentials on every storage port."""

import asyncio

import pytest
from quart import Quart, g

from tests.storage import test_storage_application_contract as storage_contract

pytestmark = pytest.mark.api
application_storage = storage_contract.application_storage


@pytest.fixture
def application_storage_owner_id():
    return 17


def test_http_and_socket_revalidate_revoked_authority_without_duplicate_reads(
    application_storage, monkeypatch,
):
    from lib.api_keys import create_key, update_key
    from lib.api_keys import _validate
    from routes.api_v1 import auth
    from routes.push import _resolve_push_ws_auth

    # A remote peer in open mode must still present a real credential. This
    # exercises the previously duplicated open -> required-gate lookup.
    monkeypatch.setattr(auth, '_mode_requires_credential', lambda: False)
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: False)
    monkeypatch.setattr(auth, 'open_mode_peer_allowed', lambda _peer: False)
    row, token = create_key(name='request-owner', scopes=['chat'], owner_user_id=17)

    reads = []
    get_client = _validate.get_storage_client
    class ReadPort:
        def __init__(self, client):
            self.client = client

        def query(self, operation, *args, **kwargs):
            reads.append(operation)
            return self.client.query(operation, *args, **kwargs)

    def counted_client(*, write=False):
        client = get_client(write=write)
        return client if write else ReadPort(client)

    monkeypatch.setattr(_validate, 'get_storage_client', counted_client)
    app = Quart(__name__)
    app.before_request(auth.auth_before_request)

    @app.post('/api/v1/keys/whoami')
    async def identity_probe():
        return {'owner': g.principal_context.owner_user_id,
                'synthetic': g.auth_ctx.via_open_mode}

    async def request(headers):
        response = await app.test_client().post('/api/v1/keys/whoami', headers=headers)
        return response.status_code, await response.get_json()

    headers = {'Authorization': 'Bearer '+token}
    status, body = asyncio.run(request(headers))
    assert status == 200 and body == {'owner': 17, 'synthetic': False}
    assert reads == ['credential.validate']

    assert update_key(row['id'], owner_user_id=17, disabled=True)
    status, _ = asyncio.run(request({**headers, 'Sec-Fetch-Site': 'cross-site'}))
    assert status == 401
    assert reads == ['credential.validate'] * 2

    context, reason = _resolve_push_ws_auth(headers, {}, ('203.0.113.7', 1))
    assert context is None and reason == 'invalid_token'
    assert reads == ['credential.validate'] * 3

    assert update_key(row['id'], owner_user_id=17, disabled=False)
    status, body = asyncio.run(request(headers))
    assert status == 200 and body == {'owner': 17, 'synthetic': False}
    assert reads == ['credential.validate'] * 4

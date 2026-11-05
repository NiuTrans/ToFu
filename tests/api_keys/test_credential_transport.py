"""Transport selection never substitutes a lower-priority identity."""

import asyncio

import pytest
from quart import Quart, g
from werkzeug.datastructures import Headers

from lib.api_keys import AuthContext
from lib.api_keys.transport import select_credential

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('headers,cookies,query,token,source', [
    (None, None, None, '', 'none'),
    ({'Authorization': 'bEaReR\t tofu_live_header  '},
     {'tofu_session': 'tofu_live_cookie'}, {'token': 'tofu_live_query'},
     'tofu_live_header', 'header'),
    ({'Authorization': 'Bearer unknown', 'x-api-key': 'tofu_live_key'},
     {'tofu_session': 'tofu_admin_cookie'}, {'token': 'tofu_live_query'},
     'unknown', 'header'),
    ({'Authorization': 'Bearer  ', 'x-api-key': ' tofu_live_key '},
     {'tofu_session': 'tofu_live_cookie'}, None, 'tofu_live_key', 'x-api-key'),
    ({'Authorization': 'Basic ignored', 'x-api-key': 'tofu_admin_key'},
     None, None, 'tofu_admin_key', 'x-api-key'),
    ({'x-api-key': 'unknown'}, {'tofu_session': ' tofu_live_cookie '},
     {'token': 'tofu_live_query'}, 'tofu_live_cookie', 'cookie'),
    ({}, {'tofu_session': 'unknown'}, {'token': ' tofu_admin_query '},
     'tofu_admin_query', 'query'),
    ({'Authorization': 'Bearer'}, {'tofu_session': 'unknown'},
     {'token': 'unknown'}, '', 'none'),
])
def test_precedence_and_explicit_invalid_bearer(headers, cookies, query, token, source):
    candidate = select_credential(headers, cookies, query)
    assert (candidate.token, candidate.source) == (token, source)


def test_transport_headers_remain_case_insensitive_and_secret_stays_out_of_repr():
    candidate = select_credential(Headers({'X-API-Key': 'tofu_live_secret'}), None)
    assert (candidate.token, candidate.source) == ('tofu_live_secret', 'x-api-key')
    assert 'tofu_live_secret' not in repr(candidate)


@pytest.mark.parametrize('authorization,expected_status,expected_token', [
    ('Bearer rejected', 401, 'rejected'),
    ('Bearer tofu_live_header', 200, 'tofu_live_header'),
    ('Basic ignored', 200, 'tofu_live_key'),
    ('', 200, 'tofu_live_key'),
])
def test_http_and_socket_resolve_the_same_candidate(
    monkeypatch, authorization, expected_status, expected_token,
):
    from routes.api_v1 import auth
    from routes.push import _resolve_push_ws_auth

    expected = AuthContext(key_id='key', owner_user_id=17, scopes=frozenset({'admin'}))
    calls = []
    def validate(token):
        calls.append(token)
        return None if token == 'rejected' else expected

    monkeypatch.setattr(auth, '_mode_requires_credential', lambda: True)
    monkeypatch.setattr('lib.auth_mode.requires_credential', lambda: True)
    monkeypatch.setattr(auth, 'validate_token', validate)
    monkeypatch.setattr('lib.api_keys.validate_token', validate)
    monkeypatch.setattr(auth, 'audit_log', lambda *_args, **_kwargs: None)
    headers = Headers({'Authorization': authorization, 'x-api-key': 'tofu_live_key',
                       'Cookie': 'tofu_session=tofu_live_cookie'})

    async def resolve_http():
        # A public probe still resolves explicit credentials, without recording
        # a private request counter. The existing middleware owns its errors.
        app = Quart(__name__)
        async with app.test_request_context('/api/v1/keys/whoami', headers=headers):
            response = await auth.auth_before_request()
            if response is not None:
                response = await app.make_response(response)
            assert (response.status_code if response is not None else 200) == expected_status
            return g.auth_ctx

    http_context = asyncio.run(resolve_http())
    socket_context, reason = _resolve_push_ws_auth(
        headers, {'tofu_session': 'tofu_live_cookie'}, ('127.0.0.1', 1))
    assert http_context is socket_context
    assert reason == ('invalid_token' if expected_status == 401 else 'token')
    assert calls == [expected_token, expected_token]


def test_http_first_link_keeps_redirect_cookie_and_other_query_values(monkeypatch):
    from routes.api_v1 import auth

    expected = AuthContext(key_id='key', owner_user_id=17, scopes=frozenset({'admin'}))
    monkeypatch.setattr(auth, '_mode_requires_credential', lambda: True)
    monkeypatch.setattr(auth, 'validate_token', lambda _token: expected)
    monkeypatch.setattr(auth, 'audit_log', lambda *_args, **_kwargs: None)

    async def first_link():
        app = Quart(__name__)
        async with app.test_request_context('/?token=tofu_live_link&view=a&view=b'):
            response = await auth.auth_before_request()
            assert response.status_code == 302
            assert response.headers['Location'] == 'http://localhost/?view=a&view=b'
            cookie = response.headers['Set-Cookie']
            assert 'tofu_session=tofu_live_link' in cookie
            assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
            assert g.auth_ctx is expected

    asyncio.run(first_link())

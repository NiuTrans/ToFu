"""Kimi Code membership sign-in: RFC 8628 device grant, refresh and expiry.

Every spec talks to a real loopback HTTP server (``_fake_kimi.FakeKimi``)
standing in for auth.kimi.com, so the wire contract (form fields, grant
types and the honest Tofu identity headers) is asserted on actual requests.
"""

from __future__ import annotations

import time
from unittest import mock

import pytest

import lib.oauth.token_store as token_store
from tests.oauth._fake_kimi import FakeKimi

pytestmark = pytest.mark.unit

DEVICE_GRANT = 'urn:ietf:params:oauth:grant-type:device_code'


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        token_store, '_config_path', lambda relative: str(tmp_path / relative))
    return tmp_path


@pytest.fixture
def fake_kimi(monkeypatch):
    from lib.oauth import kimi
    server = FakeKimi().start()
    for key, value in server.config().items():
        monkeypatch.setitem(kimi.KIMI_OAUTH_CONFIG, key, value)
    yield server
    server.stop()


def _assert_honest_identity(headers: dict, device_id: str) -> None:
    lowered = {k.lower(): v for k, v in headers.items()}
    assert lowered['x-msh-platform'] == 'tofu'
    assert lowered['user-agent'].startswith('Tofu/')
    assert lowered['x-msh-device-id'] == device_id
    for name in ('x-msh-version', 'x-msh-device-name', 'x-msh-device-model',
                 'x-msh-os-version'):
        assert lowered[name]
    # Kimi treats a tampered client identifier as a violation: Tofu never
    # presents itself as the official CLI.
    assert 'kimi_cli' not in ' '.join(lowered.values()).lower()


def test_device_request_mints_code_with_public_client_and_identity(fake_kimi):
    from lib.oauth.kimi import KIMI_OAUTH_CONFIG, kimi_device_request_code
    fake_kimi.device_replies.append((200, {
        'device_code': 'dev-123', 'user_code': 'ABCD-EFGH',
        'verification_uri': 'https://www.kimi.com/code/authorize_device',
        'verification_uri_complete':
            'https://www.kimi.com/code/authorize_device?user_code=ABCD-EFGH',
        'interval': 5, 'expires_in': 1800,
    }))

    out = kimi_device_request_code(device_id='device-1')

    assert out == {
        'device_code': 'dev-123', 'user_code': 'ABCD-EFGH',
        'verification_url':
            'https://www.kimi.com/code/authorize_device?user_code=ABCD-EFGH',
        'interval': 5,
        # A longer upstream lifetime is capped at the 15-minute flow ceiling.
        'expires_in': KIMI_OAUTH_CONFIG['device_flow_timeout'],
    }
    sent = fake_kimi.last('/api/oauth/device_authorization')
    assert sent['body'] == {'client_id': KIMI_OAUTH_CONFIG['client_id']}
    _assert_honest_identity(sent['headers'], 'device-1')


def test_device_request_rejects_non_https_verification_link(fake_kimi):
    from lib.oauth.kimi import kimi_device_request_code
    from lib.oauth.token_store import OAuthExchangeError
    fake_kimi.device_replies.append((200, {
        'device_code': 'd', 'user_code': 'U',
        'verification_uri_complete': 'javascript:alert(1)'}))
    with pytest.raises(OAuthExchangeError):
        kimi_device_request_code(device_id='device-1')


def test_device_request_unreachable_is_transient(monkeypatch):
    from lib.oauth import kimi
    from lib.oauth.token_store import OAuthExchangeError
    monkeypatch.setitem(kimi.KIMI_OAUTH_CONFIG, 'device_authorization_url',
                        'http://127.0.0.1:9/api/oauth/device_authorization')
    with pytest.raises(OAuthExchangeError) as raised:
        kimi.kimi_device_request_code(device_id='device-1')
    assert raised.value.status_code == 0


def test_poll_reports_pending_and_slow_down_then_returns_unsaved_token(
        fake_kimi, private_store):
    from lib.oauth.kimi import (
        DEVICE_PENDING, DEVICE_SLOW_DOWN, kimi_device_poll_token)
    fake_kimi.token_replies.extend([
        (200, {'error': 'authorization_pending'}),   # Kimi's 200 + error
        (400, {'error': 'slow_down'}),               # RFC 8628 400 form
        (200, {'access_token': 'at-1', 'refresh_token': 'rt-1',
               'token_type': 'Bearer', 'expires_in': 3600, 'scope': 'kimi'}),
    ])

    assert kimi_device_poll_token('dev-123', device_id='device-1') == DEVICE_PENDING
    assert kimi_device_poll_token('dev-123', device_id='device-1') == DEVICE_SLOW_DOWN
    record = kimi_device_poll_token('dev-123', device_id='device-1')

    assert record['access_token'] == 'at-1'
    assert record['refresh_token'] == 'rt-1'
    assert record['device_id'] == 'device-1'
    assert record['expire'] == pytest.approx(time.time() + 3600, abs=30)
    # Polling never persists: the manager saves only a current generation.
    assert token_store.load_token('kimi') is None
    sent = fake_kimi.last('/api/oauth/token')
    assert sent['body']['grant_type'] == DEVICE_GRANT
    assert sent['body']['device_code'] == 'dev-123'
    _assert_honest_identity(sent['headers'], 'device-1')


@pytest.mark.parametrize('error', ['expired_token', 'access_denied', 'invalid_client'])
def test_poll_terminal_errors_raise_non_transient(fake_kimi, error):
    from lib.oauth.kimi import kimi_device_poll_token
    from lib.oauth.token_store import OAuthExchangeError
    fake_kimi.token_replies.append((400, {'error': error}))
    with pytest.raises(OAuthExchangeError) as raised:
        kimi_device_poll_token('dev-123', device_id='device-1')
    assert raised.value.status_code != 0


def test_poll_server_error_is_transient(fake_kimi):
    from lib.oauth.kimi import kimi_device_poll_token
    from lib.oauth.token_store import OAuthExchangeError
    fake_kimi.token_replies.append((503, {'message': 'busy'}))
    with pytest.raises(OAuthExchangeError) as raised:
        kimi_device_poll_token('dev-123', device_id='device-1')
    assert raised.value.status_code == 0


def _store(access='at-1', refresh='rt-1', expire_in=3600, **extra):
    from lib.oauth.kimi import kimi_save_login_token
    return kimi_save_login_token({
        'type': 'kimi', 'access_token': access, 'refresh_token': refresh,
        'expire': time.time() + expire_in, 'expires_in': expire_in,
        'device_id': 'device-1', 'email': '', **extra})


def test_valid_token_is_returned_without_network(fake_kimi, private_store):
    from lib.oauth.kimi import kimi_get_valid_token
    _store()
    assert kimi_get_valid_token() == 'at-1'
    assert fake_kimi.paths() == []


def test_refresh_near_expiry_rotates_and_persists(fake_kimi, private_store):
    from lib.oauth.kimi import KIMI_OAUTH_CONFIG, kimi_get_valid_token
    _store(expire_in=60)  # inside the five-minute refresh window
    fake_kimi.token_replies.append((200, {
        'access_token': 'at-2', 'refresh_token': 'rt-2', 'expires_in': 3600}))

    assert kimi_get_valid_token() == 'at-2'

    sent = fake_kimi.last('/api/oauth/token')
    assert sent['body'] == {'client_id': KIMI_OAUTH_CONFIG['client_id'],
                            'grant_type': 'refresh_token',
                            'refresh_token': 'rt-1'}
    _assert_honest_identity(sent['headers'], 'device-1')
    stored = token_store.load_token('kimi')
    assert stored['access_token'] == 'at-2'
    assert stored['refresh_token'] == 'rt-2'
    assert stored['device_id'] == 'device-1'


def test_rejected_refresh_after_expiry_requires_sign_in(fake_kimi, private_store):
    from lib.oauth.kimi import kimi_get_valid_token
    _store(expire_in=-10)  # already expired
    fake_kimi.token_replies.append((401, {'error': 'invalid_grant'}))

    assert kimi_get_valid_token() is None

    stored = token_store.load_token('kimi')
    assert stored['refresh_token'] == ''
    assert stored['refresh_invalidated_at']
    # A terminal rejection is never replayed on the next request.
    assert kimi_get_valid_token() is None
    assert fake_kimi.paths().count('/api/oauth/token') == 1


def test_transient_refresh_failure_keeps_unexpired_token(fake_kimi, private_store):
    from lib.oauth.kimi import kimi_get_valid_token
    _store(expire_in=120)
    fake_kimi.token_replies.extend([(502, {}), (502, {}), (502, {})])
    with mock.patch('lib.oauth.kimi.time.sleep'):
        assert kimi_get_valid_token() == 'at-1'
    assert token_store.load_token('kimi')['refresh_token'] == 'rt-1'


def test_unknown_lifetime_is_used_until_rejected(fake_kimi, private_store):
    from lib.oauth.kimi import kimi_get_valid_token, kimi_save_login_token
    kimi_save_login_token({'type': 'kimi', 'access_token': 'at-1',
                           'refresh_token': 'rt-1', 'expire': 0,
                           'device_id': 'device-1'})
    assert kimi_get_valid_token() == 'at-1'
    assert fake_kimi.paths() == []

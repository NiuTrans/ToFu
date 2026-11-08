"""Kimi Code subscription sign-in through the shared subscription owners.

Covers the Settings → Subscription sign-in contract end to end against a
loopback fake of auth.kimi.com and the Kimi Code chat endpoint: device-code
sign-in with status projection, sign-out, the managed ``oauth_kimi``
provider (roster from the ``kimi_code`` plan template), live-token request
resolution with honest identity headers, a real OpenAI-protocol request on
the wire, the typed sign-in-required failure, forced refresh after a 401,
and Kimi's quota-exhausted 429 bodies on the subscription-quota channel.
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest import mock

import pytest

import lib.oauth.token_store as token_store
from tests.oauth._fake_kimi import FakeKimi

pytestmark = pytest.mark.unit


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


@pytest.fixture
def flows():
    import lib.oauth.manager as mgr
    yield mgr
    for provider in list(mgr._active_flows):
        mgr.stop_device_flow(provider)
    with mgr._flows_lock:
        mgr._active_flows.clear()


def _store_login(access='at-1', refresh='rt-1', expire_in=3600):
    from lib.oauth.kimi import kimi_save_login_token
    return kimi_save_login_token({
        'type': 'kimi', 'access_token': access, 'refresh_token': refresh,
        'expire': time.time() + expire_in, 'expires_in': expire_in,
        'device_id': 'device-1', 'email': ''})


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── Sign-in, status and sign-out through the subscription manager ─────


def test_device_sign_in_projects_code_then_provisions(fake_kimi, private_store, flows):
    fake_kimi.device_replies.append((200, {
        'device_code': 'dev-secret', 'user_code': 'WXYZ-1234',
        'verification_uri_complete': 'https://www.kimi.com/code/authorize_device?user_code=WXYZ-1234',
        'interval': 1, 'expires_in': 600}))
    fake_kimi.token_replies.extend([
        (200, {'error': 'authorization_pending'}),
        (200, {'access_token': 'at-1', 'refresh_token': 'rt-1', 'expires_in': 3600}),
    ])
    with mock.patch('lib.oauth.outbound.provision_oauth_provider') as provision, \
         mock.patch('lib.oauth.outbound.managed_oauth_provider_status',
                    return_value={'provider_ready': True}):
        started = flows.start_device_flow('kimi', owner_user_id=1)
        assert started['user_code'] == 'WXYZ-1234'
        assert started['verification_url'].startswith('https://www.kimi.com/')
        assert 'dev-secret' not in repr(started)

        status = flows.get_oauth_status('kimi', owner_user_id=1)
        assert status['device'] == {
            'user_code': 'WXYZ-1234',
            'verification_url': started['verification_url']}
        assert 'dev-secret' not in repr(status)

        assert _wait(lambda: (token_store.load_token('kimi') or {}).get('access_token'))
        assert _wait(lambda: provision.called)
        provision.assert_called_with('kimi', owner_user_id=1)
        assert _wait(lambda: flows.get_oauth_status(
            'kimi', owner_user_id=1)['status'] == 'success')
        final = flows.get_oauth_status('kimi', owner_user_id=1)
    assert final['authenticated'] is True
    assert final['expire'] == pytest.approx(time.time() + 3600, abs=60)
    # The device identifier is minted once and travels with the login.
    device_ids = {row['headers'].get('X-Msh-Device-Id') for row in fake_kimi.requests}
    assert len(device_ids) == 1
    assert token_store.load_token('kimi')['device_id'] in device_ids


def test_denied_device_sign_in_reports_error(fake_kimi, private_store, flows):
    fake_kimi.device_replies.append((200, {
        'device_code': 'd', 'user_code': 'U-1',
        'verification_uri': 'https://www.kimi.com/code/authorize_device',
        'interval': 1, 'expires_in': 600}))
    fake_kimi.token_replies.append((400, {'error': 'access_denied'}))
    with mock.patch('lib.oauth.outbound.managed_oauth_provider_status',
                    return_value={}):
        flows.start_device_flow('kimi', owner_user_id=1)
        assert _wait(lambda: flows.get_oauth_status(
            'kimi', owner_user_id=1)['status'] == 'error')
    assert token_store.load_token('kimi') is None


def test_sign_out_deletes_login_and_deprovisions(fake_kimi, private_store, flows):
    _store_login()
    with mock.patch('lib.oauth.outbound.deprovision_oauth_provider') as deprovision, \
         mock.patch('lib.oauth.outbound.managed_oauth_provider_status',
                    return_value={}):
        result = flows.logout_oauth('kimi', owner_user_id=1)
        status = flows.get_oauth_status('kimi', owner_user_id=1)
    assert result == {'ok': True, 'provider': 'kimi'}
    deprovision.assert_called_once_with('kimi', owner_user_id=1)
    assert token_store.load_token('kimi') is None
    assert status['authenticated'] is False


def test_expired_login_without_refresh_needs_sign_in(private_store):
    from lib.oauth.manager import get_oauth_status
    _store_login(refresh='', expire_in=-5)
    with mock.patch('lib.oauth.outbound.managed_oauth_provider_status',
                    return_value={}):
        status = get_oauth_status('kimi', owner_user_id=1)
    assert status['authenticated'] is False
    assert status['auth_issue'] == 'access_expired_no_refresh'


# ── Managed provider, request resolution and the wire ─────────────────


def test_managed_provider_uses_the_kimi_code_plan_roster():
    from lib.oauth.outbound import OAUTH_PROVIDERS, _managed_oauth_entry
    assert 'kimi' in OAUTH_PROVIDERS
    entry = _managed_oauth_entry('kimi')
    assert entry['id'] == 'oauth_kimi'
    assert entry['oauth'] == 'kimi'
    assert entry['base_url'] == 'https://api.kimi.com/coding/v1'
    assert entry['protocol'] in ('', 'openai')
    wires = {wire for row in entry['models'] for wire in row.get('request_ids') or []}
    assert {'k3.1-pro', 'k3-256k', 'kimi-for-coding'} <= wires
    k31 = next(row for row in entry['models'] if row['model_id'] == 'kimi-k3.1-pro')
    assert k31['context_window'] == 262144


def test_resolve_attaches_live_token_and_honest_identity(private_store):
    from lib.oauth.outbound import resolve_oauth_request
    _store_login()
    token, headers, body = resolve_oauth_request(
        'kimi', {'model': 'k3.1-pro', 'messages': []}, {'X-Caller': '1'},
        user_id='1')
    assert token == 'at-1'
    assert headers['X-Caller'] == '1'
    assert headers['X-Msh-Platform'] == 'tofu'
    assert headers['X-Msh-Device-Id'] == 'device-1'
    assert headers['User-Agent'].startswith('Tofu/')
    assert body == {'model': 'k3.1-pro', 'messages': []}


def test_missing_login_raises_typed_sign_in_required(private_store):
    from lib.llm.llm_errors import SubscriptionLoginRequiredError
    from lib.oauth.outbound import resolve_oauth_request
    with pytest.raises(SubscriptionLoginRequiredError) as raised:
        resolve_oauth_request('kimi', {'messages': []}, {}, user_id='1')
    from lib.error_envelope import _classify_exception
    assert _classify_exception(raised.value) == 'oauth_reauth'


def test_chat_request_rides_bearer_token_on_the_coding_endpoint(
        fake_kimi, private_store, monkeypatch):
    from lib.llm.chat import chat
    _store_login()
    fake_kimi.chat_replies.append((200, {
        'id': 'c1', 'object': 'chat.completion', 'model': 'k3.1-pro',
        'choices': [{'index': 0, 'finish_reason': 'stop',
                     'message': {'role': 'assistant', 'content': 'pong'}}],
        'usage': {'prompt_tokens': 3, 'completion_tokens': 1, 'total_tokens': 4}}))

    content, usage = chat([{'role': 'user', 'content': 'ping'}], model='k3.1-pro',
                          knowledge_model='kimi-k3.1-pro',
                          base_url=fake_kimi.origin + '/coding/v1',
                          api_protocol='openai', oauth='kimi', owner_user_id=1,
                          max_retries=0)

    assert content == 'pong'
    sent = fake_kimi.last('/coding/v1/chat/completions')
    lowered = {k.lower(): v for k, v in sent['headers'].items()}
    assert lowered['authorization'] == 'Bearer at-1'
    assert lowered['x-msh-platform'] == 'tofu'
    assert lowered['user-agent'].startswith('Tofu/')
    assert sent['body']['model'] == 'k3.1-pro'
    assert sent['body']['messages'][-1]['content'] == 'ping'


def test_forced_refresh_after_401_uses_the_kimi_grant(fake_kimi, private_store):
    from lib.llm_dispatch._api_budget import _force_oauth_token_refresh
    _store_login()
    fake_kimi.token_replies.append((200, {
        'access_token': 'at-2', 'refresh_token': 'rt-2', 'expires_in': 3600}))
    assert _force_oauth_token_refresh('kimi', '[t]', 1) is True
    assert token_store.load_token('kimi')['access_token'] == 'at-2'


@pytest.mark.parametrize('body', [
    "You've reached your 5-hour usage limit. Your quota will reset when the current 5-hour window ends.",
    "You've reached your weekly (7-day) usage limit. Your quota will reset when the current 7-day window ends.",
    "You've reached your monthly usage limit for this billing cycle. Your quota will be refreshed in the next cycle.",
])
def test_kimi_quota_bodies_use_the_subscription_quota_channel(body):
    from lib.llm.llm_errors import RateLimitError, is_subscription_quota_error
    assert is_subscription_quota_error(body)
    assert RateLimitError(body, status_code=429).is_subscription_quota is True


def test_kimi_credit_balance_exhaustion_is_billing_quota():
    from lib.llm.llm_errors import _is_quota_exhausted
    assert _is_quota_exhausted(
        'Your credit balance is insufficient. To continue, purchase extra usage')


# ── HTTP surfaces ─────────────────────────────────────────────────────


def _app(*blueprints):
    import quart as _quart
    sys.modules.setdefault('flask', _quart)
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    app = Quart(__name__)
    for blueprint in blueprints:
        app.register_blueprint(blueprint)

    @app.before_request
    def _fake_auth():
        from quart import g
        from lib.api_keys import local_admin_context
        g.auth_ctx = local_admin_context()
    return app


def test_device_login_and_logout_routes_accept_kimi():
    from routes.api_v1.oauth import api_v1_oauth_bp
    from routes.oauth import oauth_bp
    app = _app(api_v1_oauth_bp, oauth_bp)

    async def _run():
        client = app.test_client()
        with mock.patch('lib.oauth.manager.start_device_flow',
                        return_value={'status': 'started', 'provider': 'kimi',
                                      'user_code': 'U', 'verification_url': 'https://x',
                                      'interval': 5, 'expires_in': 600}) as start:
            response = await client.post('/api/v1/oauth/device-login',
                                         json={'provider': 'kimi'})
            assert response.status_code == 200
            assert start.call_args.args[0] == 'kimi'
        with mock.patch('lib.oauth.manager.logout_oauth',
                        return_value={'ok': True, 'provider': 'kimi'}) as logout:
            response = await client.post('/api/oauth/logout', json={'provider': 'kimi'})
            assert response.status_code == 200
            assert logout.call_args.args[0] == 'kimi'
        with mock.patch('lib.oauth.manager.get_oauth_status',
                        return_value={'provider': 'kimi'}), \
             mock.patch('routes.api_v1.oauth._reconcile_oauth_throttled',
                        return_value=None):
            response = await client.get('/api/v1/oauth/status?provider=kimi')
            assert response.status_code == 200

    asyncio.run(_run())

"""Health is liveness, never authority to forward a saved bridge bearer."""
import asyncio

import pytest
import requests

from lib.desktop_agent import _pair, _probe

pytestmark = pytest.mark.unit


def test_real_bridge_rejection_contract():
    from quart import Quart
    from routes._bridge_caller import bridge_unauthorized

    async def response_body():
        async with Quart(__name__).app_context():
            response, status = bridge_unauthorized()
            assert status == 401
            return await response.get_json()

    body = asyncio.run(response_body())
    assert body['error'] == 'bridge_auth_required'
    assert _probe.is_tofu_error_envelope(body)
    assert not _probe.is_tofu_error_envelope({'error': 'Unauthorized'})
    assert not _probe.is_tofu_error_envelope({'error': 'bridge_auth_required_extra'})


@pytest.mark.parametrize('discovered', [
    'http://192.168.1.99:15000', 'http://127.0.0.1:15000',
    'https://another-tofu.example',
])
def test_resume_never_authorizes_discovery_with_existing_bearer(monkeypatch, discovered):
    from lib.desktop_agent import config
    monkeypatch.setattr(config, 'load_config', lambda: {'attach_candidates': []})
    monkeypatch.setattr(config, 'save_remote_server',
                        lambda *a: pytest.fail('must not persist discovery'))
    monkeypatch.setattr(_pair, 'discover',
                        lambda **k: pytest.fail('must not discover bearer destinations'))
    monkeypatch.setattr(_pair, 'probe_server',
                        lambda url, **k: (url == discovered, 'unreachable'))
    assert _pair.resume_attachment('https://saved.example', 'synthetic-token') == (
        'https://saved.example', 'synthetic-token')


def test_explicit_attach_candidate_remains_authorized(monkeypatch):
    from lib.desktop_agent import config
    candidate = 'https://configured.example/tofu'
    monkeypatch.setattr(config, 'load_config',
                        lambda: {'attach_candidates': [candidate]})
    saved = []
    monkeypatch.setattr(config, 'save_remote_server', lambda *args: saved.append(args))
    monkeypatch.setattr(_pair, 'probe_server', lambda url, **k: (url == candidate, ''))
    assert _pair.resume_attachment('https://old.example', 'token') == (candidate, 'token')
    assert saved == [(candidate, 'token')]


@pytest.mark.parametrize('url', [
    'http://user:password@example.com', 'https://example.com?token=secret',
    'https://example.com/#fragment', 'file:///tmp/server',
    'https://example.com:bad', 'https://example.com\\@evil.example',
])
def test_unsafe_probe_urls_never_make_request(monkeypatch, url):
    monkeypatch.setattr(requests, 'get', lambda *a, **k: pytest.fail('unsafe URL'))
    assert _probe.probe_server(url) == (False, 'unreachable')


def test_probe_honors_proxy_ca_and_suppresses_ambient_auth(monkeypatch):
    for key in ('https_proxy', 'http_proxy', 'all_proxy', 'no_proxy'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.example:3128')
    monkeypatch.setenv('NO_PROXY', 'localhost')
    monkeypatch.setenv('REQUESTS_CA_BUNDLE', '/synthetic/company-ca.pem')
    monkeypatch.setattr(requests.sessions, 'get_netrc_auth',
                        lambda *a: ('ambient-user', 'ambient-password'))
    observed = {}

    def send(self, request, **kwargs):
        observed.update(kwargs)
        assert 'Authorization' not in request.headers
        assert 'Cookie' not in request.headers
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"bootId":"public-not-an-identity"}'
        return response

    monkeypatch.setattr(requests.Session, 'send', send)
    assert _probe.probe_server('https://server.example') == (True, '')
    assert observed['proxies']['https'] == 'http://proxy.example:3128'
    assert observed['verify'] == '/synthetic/company-ca.pem'
    assert observed['allow_redirects'] is False


def test_read_timeout_is_timeout(monkeypatch):
    def get(*a, **k):
        raise requests.exceptions.ReadTimeout()
    monkeypatch.setattr(requests, 'get', get)
    assert _probe.probe_server('https://server.example') == (False, 'timeout')


def test_tls_failure_is_not_retried_without_verification(monkeypatch):
    calls = []
    def get(*a, **k):
        calls.append(k)
        raise requests.exceptions.SSLError('untrusted certificate')
    monkeypatch.setattr(requests, 'get', get)
    assert _probe.probe_server('https://server.example') == (False, 'unreachable')
    assert len(calls) == 1
    assert calls[0].get('verify', True) is True

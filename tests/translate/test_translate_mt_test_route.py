"""POST /api/v1/translate/mt-test keeps its documented 200 carve-out typed.

docs/contracts/API_CONTRACT.md registers one deliberate legacy exception: the
machine-translation provider test reports a provider's *logical* failure with
HTTP 200 and ``ok: false`` (the Settings panel prints ``data.error``). Only a
typed :class:`lib.mt_provider.MtProviderError` qualifies. A server fault is a
logged, redacted 500 and a malformed caller field is a 400; neither may ride
the 200 carve-out with raw exception text.
"""

from __future__ import annotations

import asyncio
import json

import pytest

pytestmark = pytest.mark.unit

_PATH = '/api/v1/translate/mt-test'
_CONFIG = {'api_key': 'k-test', 'app_id': '', 'api_url': ''}


def _post(body):
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    from lib.http_transport.error_handlers import register_http_error_handlers
    from routes.api_v1 import translate as translate_route

    app = Quart(__name__)
    register_http_error_handlers(app)

    @app.before_request
    def _bind_test_owner():
        g.auth_ctx = local_admin_context()

    app.register_blueprint(translate_route.api_v1_translate_bp)

    async def _request():
        async with app.test_client() as client:
            response = await client.post(_PATH, json=body)
            return response.status_code, await response.get_data(as_text=True)

    status, raw = asyncio.run(_request())
    return status, json.loads(raw), raw


def _provider(monkeypatch, behaviour):
    calls = []

    def fake(*args, **kwargs):
        calls.append(args)
        return behaviour()

    monkeypatch.setattr('lib.mt_provider._niutrans_v1', fake)
    monkeypatch.setattr('lib.mt_provider._niutrans_v2', fake)
    return calls


def test_provider_failure_keeps_documented_200_carve_out(monkeypatch):
    from lib.mt_provider import MtProviderError

    def reject():
        raise MtProviderError('NiuTrans API error 13001: apikey invalid')

    _provider(monkeypatch, reject)
    status, body, _raw = _post({'mt_config': _CONFIG})
    assert status == 200
    assert body['ok'] is False
    assert body['error'] == 'NiuTrans API error 13001: apikey invalid'


def test_missing_api_key_keeps_documented_200_carve_out(monkeypatch):
    calls = _provider(monkeypatch, lambda: 'unused')
    status, body, _raw = _post({'mt_config': {}})
    assert status == 200
    assert body['ok'] is False
    assert body['error'] == 'API Key 未填写'
    assert calls == []


@pytest.mark.parametrize('app_id', ['', 'app-1'])
def test_success_returns_the_translation(monkeypatch, app_id):
    calls = _provider(monkeypatch, lambda: '你好')
    status, body, _raw = _post({'mt_config': {**_CONFIG, 'app_id': app_id}})
    assert status == 200
    assert body['ok'] is True
    assert body['translated'] == '你好'
    assert len(calls) == 1


@pytest.mark.parametrize('fault', [
    KeyError('internal-secret-detail'),
    RuntimeError('internal-secret-detail'),
])
def test_server_fault_is_a_redacted_500_not_the_200_carve_out(monkeypatch, fault):
    def crash():
        raise fault

    _provider(monkeypatch, crash)
    status, body, raw = _post({'mt_config': _CONFIG})
    assert status == 500
    assert body['ok'] is False
    assert 'internal-secret-detail' not in raw


@pytest.mark.parametrize(('body', 'field'), [
    ({'mt_config': 'k-test'}, 'mt_config'),
    ({'mt_config': {'api_key': 7}}, 'api_key'),
    ({'mt_config': {**_CONFIG, 'api_url': 9}}, 'api_url'),
    ({'mt_config': {**_CONFIG, 'app_id': ['a']}}, 'app_id'),
    ({'mt_config': _CONFIG, 'source': 7}, 'source'),
    ({'mt_config': _CONFIG, 'target': {'x': 1}}, 'target'),
    ({'mt_config': _CONFIG, 'text': ['hello']}, 'text'),
])
def test_malformed_caller_field_is_a_400(monkeypatch, body, field):
    calls = _provider(monkeypatch, lambda: 'unused')
    status, payload, _raw = _post(body)
    assert status == 400
    assert payload['ok'] is False
    assert payload.get('field') == field
    assert calls == []


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _adapter(name):
    from lib.mt_provider import _niutrans

    if name == 'v1':
        return lambda: _niutrans._niutrans_v1('hello', 'en', 'zh', 'k')
    return lambda: _niutrans._niutrans_v2('hello', 'en', 'zh', 'k', 'app')


@pytest.mark.parametrize('version', ['v1', 'v2'])
@pytest.mark.parametrize('payload', [
    ['unexpected'], 'unexpected', {'error_code': '13001', 'errorCode': '13001',
                                   'error_msg': 'apikey invalid',
                                   'errorMsg': 'apikey invalid'},
    {'tgt_text': '', 'tgtText': ''},
])
def test_provider_answers_are_typed_provider_failures(monkeypatch, version, payload):
    from lib.mt_provider import MtProviderError, _niutrans

    monkeypatch.setattr(_niutrans, 'http_post', lambda *a, **k: _Response(payload))
    with pytest.raises(MtProviderError):
        _adapter(version)()


@pytest.mark.parametrize('version', ['v1', 'v2'])
def test_provider_transport_failure_is_a_typed_provider_failure(monkeypatch, version):
    import requests
    from lib.mt_provider import MtProviderError, _niutrans

    def refuse(*_args, **_kwargs):
        raise requests.ConnectionError('connection refused')

    monkeypatch.setattr(_niutrans, 'http_post', refuse)
    with pytest.raises(MtProviderError):
        _adapter(version)()


def test_provider_failure_stays_a_runtime_error_for_in_process_callers():
    from lib.mt_provider import MtProviderError

    assert issubclass(MtProviderError, RuntimeError)

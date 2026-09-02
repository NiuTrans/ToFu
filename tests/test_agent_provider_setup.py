"""Behavior contract for the database-free Provider setup control plane."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self, provider=None, *, source='none'):
        self.principal = SimpleNamespace(subject_id='setup:test')
        self.provider = provider
        self.provider_source = source
        self.default_model = provider.model if provider else ''
        self.closed = False
        self.capacity = 4
        self.in_flight = 0
        self.configurations = []

    def configure_provider(self, provider, *, source='runtime'):
        self.provider = provider
        self.provider_source = source
        self.default_model = provider.model if provider else ''
        self.configurations.append((provider, source))

    def get(self, _task_id):
        return None


class _Response:
    def __init__(self, status_code: int, body):
        self.status_code = status_code
        self._body = body
        if isinstance(body, bytes):
            self.content = body
        elif isinstance(body, Exception):
            self.content = b'not-json'
        else:
            self.content = json.dumps(body).encode('utf-8')

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        if isinstance(self._body, bytes):
            return json.loads(self._body)
        return self._body


def _run(coro):
    return asyncio.run(coro)


def _app(tmp_path: Path, *, token='', editable=True, provider=None,
         source='none', setup_enabled=True):
    from tofu_agent.provider_setup import ProviderSetupService
    from tofu_agent.provider_store import ProviderSettingsStore
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime(provider, source=source)
    store = ProviderSettingsStore(tmp_path / 'provider.json', environ={})
    service = ProviderSetupService(
        runtime, store, source=source, editable=editable)
    app = create_app(
        runtime=runtime,
        provider_setup=service,
        config=HeadlessServerConfig(
            bind_host='0.0.0.0' if token else '127.0.0.1',
            token=token,
            auth_mode='token' if token else 'open',
            setup_enabled=setup_enabled,
        ),
    )
    app.config['TESTING'] = True
    return app, runtime, store


def test_encrypted_store_round_trip_permissions_and_delete(tmp_path):
    from tofu_agent import ProviderConfig
    from tofu_agent.provider_store import ProviderSettingsStore

    store = ProviderSettingsStore(tmp_path / 'nested/provider.json', environ={})
    provider = ProviderConfig(
        base_url='https://models.example/v1',
        api_key='sk-store-secret-never-plaintext',
        model='model-a',
        extra_headers={'X-Gateway-Key': 'header-secret-never-plaintext'},
        thinking_format='reasoning_content',
        capabilities=frozenset({'text', 'thinking'}),
    )
    store.save(provider)

    raw = store.path.read_text(encoding='utf-8')
    assert 'sk-store-secret-never-plaintext' not in raw
    assert 'header-secret-never-plaintext' not in raw
    assert provider.base_url in raw
    assert provider.model in raw
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600

    restored = ProviderSettingsStore(store.path, environ={}).load()
    assert restored == provider
    assert store.delete() is True
    assert store.delete() is False
    assert not store.path.exists()
    assert store.key_path.exists(), 'stable key survives a config reset'


def test_provider_store_path_honors_explicit_and_xdg_configuration(tmp_path):
    from tofu_agent.provider_store import (
        default_provider_config_path, secret_hint,
    )

    explicit = tmp_path / 'explicit/provider.json'
    assert default_provider_config_path({
        'TOFU_AGENT_CONFIG_PATH': str(explicit),
        'XDG_CONFIG_HOME': str(tmp_path / 'ignored'),
    }) == explicit
    assert default_provider_config_path({
        'XDG_CONFIG_HOME': str(tmp_path / 'xdg'),
    }) == tmp_path / 'xdg/tofu-agent/provider.json'
    assert secret_hint('short-secret') == '••••'
    assert secret_hint('sk-long-private-key-1234567890') == 'sk-l…7890'


def test_encrypted_store_retries_partial_operating_system_writes(
    tmp_path, monkeypatch,
):
    from tofu_agent import ProviderConfig
    import tofu_agent.provider_store as provider_store

    real_write = provider_store.os.write

    def partial_write(descriptor, payload):
        return real_write(descriptor, payload[:7])

    monkeypatch.setattr(provider_store.os, 'write', partial_write)
    store = provider_store.ProviderSettingsStore(
        tmp_path / 'provider.json', environ={})
    expected = ProviderConfig(
        base_url='https://models.example/v1',
        api_key='partial-write-secret',
        model='model-partial-write',
    )
    store.save(expected)

    assert store.load() == expected


def test_injected_encryption_key_supports_secret_managed_deployments(tmp_path):
    from cryptography.fernet import Fernet
    from tofu_agent import ProviderConfig
    from tofu_agent.provider_store import ProviderSettingsStore, ProviderStoreError

    path = tmp_path / 'provider.json'
    key = Fernet.generate_key().decode('ascii')
    expected = ProviderConfig(
        base_url='https://models.example/v1',
        api_key='externally-keyed-secret',
        model='model-a',
    )
    store = ProviderSettingsStore(
        path, environ={'TOFU_AGENT_CONFIG_KEY': key})
    store.save(expected)

    assert not store.key_path.exists()
    assert ProviderSettingsStore(
        path, environ={'TOFU_AGENT_CONFIG_KEY': key}).load() == expected
    with pytest.raises(ProviderStoreError, match='could not be decrypted'):
        ProviderSettingsStore(path, environ={
            'TOFU_AGENT_CONFIG_KEY': Fernet.generate_key().decode('ascii'),
        }).load()


def test_store_fails_closed_when_cipher_key_is_missing_or_document_corrupt(
    tmp_path,
):
    from tofu_agent import ProviderConfig
    from tofu_agent.provider_store import ProviderSettingsStore, ProviderStoreError

    store = ProviderSettingsStore(tmp_path / 'provider.json', environ={})
    store.save(ProviderConfig(
        base_url='https://models.example/v1', api_key='secret', model='m'))
    store.key_path.unlink()
    with pytest.raises(ProviderStoreError, match='key is missing'):
        store.load()

    store.path.write_text('{broken', encoding='utf-8')
    with pytest.raises(ProviderStoreError, match='invalid JSON'):
        store.load()


def test_setup_page_is_public_but_remote_configuration_requires_token(
    tmp_path,
):
    app, _runtime, _store = _app(tmp_path, token='sidecar-secret')

    async def scenario():
        client = app.test_client()
        page = await client.get('/setup')
        assert page.status_code == 200
        assert 'Configure the model once' in await page.get_data(as_text=True)
        assert page.headers['Cache-Control'] == 'no-store'
        assert "default-src 'none'" in page.headers['Content-Security-Policy']
        assert page.headers['Referrer-Policy'] == 'no-referrer'

        javascript = await client.get('/setup/assets/setup.js')
        assert javascript.status_code == 200
        assert 'text/javascript' in javascript.content_type
        assert 'localStorage' not in await javascript.get_data(as_text=True)

        denied = await client.get('/api/v1/setup/provider')
        wrong = await client.get(
            '/api/v1/setup/provider',
            headers={'Authorization': 'Bearer wrong'})
        assert denied.status_code == wrong.status_code == 401

        allowed = await client.get(
            '/api/v1/setup/provider',
            headers={'Authorization': 'Bearer sidecar-secret'})
        assert allowed.status_code == 200
        body = await allowed.get_json()
        assert body['configured'] is False
        assert body['editable'] is True
        assert len(body['templates']) >= 5

        cross_site = await client.get(
            '/api/v1/setup/provider',
            headers={
                'Authorization': 'Bearer sidecar-secret',
                'Origin': 'https://attacker.example',
                'Sec-Fetch-Site': 'cross-site',
            },
        )
        assert cross_site.status_code == 403
        assert (await cross_site.get_json())['error']['kind'] \
            == 'cross_site_request'

    _run(scenario())


def test_save_is_secret_free_hot_applied_preserved_and_restartable(tmp_path):
    app, runtime, store = _app(tmp_path)
    secret = 'sk-hot-apply-never-return'

    async def scenario():
        client = app.test_client()
        before = await client.get('/health/ready')
        assert before.status_code == 503
        assert (await before.get_json())['setup_required'] is True

        saved = await client.put('/api/v1/setup/provider', json={
            'endpoint': 'http://127.0.0.1:18889/v1/chat/completions',
            'api_key': secret,
            'model': 'mock-model',
        })
        assert saved.status_code == 200
        saved_text = await saved.get_data(as_text=True)
        assert secret not in saved_text
        saved_body = await saved.get_json()
        assert saved_body['provider']['base_url'] \
            == 'http://127.0.0.1:18889/v1'
        assert saved_body['provider']['has_api_key'] is True
        assert saved_body['provider']['api_key_hint']
        assert runtime.default_model == 'mock-model'
        assert runtime.configurations[-1][1] == 'saved'

        after = await client.get('/health/ready')
        assert after.status_code == 200
        assert (await after.get_json())['setup_required'] is False

        redacted = await client.get('/api/v1/setup/provider')
        redacted_text = await redacted.get_data(as_text=True)
        assert secret not in redacted_text
        assert 'secret_envelope' not in redacted_text

        preserved = await client.put('/api/v1/setup/provider', json={
            'base_url': 'http://127.0.0.1:18889/v1',
            'model': 'mock-model-2',
        })
        assert preserved.status_code == 200
        assert runtime.provider.api_key == secret
        assert runtime.default_model == 'mock-model-2'

        unsafe_reuse = await client.put('/api/v1/setup/provider', json={
            'base_url': 'http://127.0.0.1:18890/v1',
            'model': 'other-model',
        })
        assert unsafe_reuse.status_code == 400
        assert 're-enter the API key' in await unsafe_reuse.get_data(as_text=True)
        assert runtime.provider.base_url.endswith(':18889/v1')

        cleared = await client.put('/api/v1/setup/provider', json={
            'base_url': 'http://127.0.0.1:18889/v1',
            'api_key': '',
            'model': 'mock-model-2',
        })
        assert cleared.status_code == 200
        assert runtime.provider.api_key == ''

    _run(scenario())

    raw = store.path.read_text(encoding='utf-8')
    assert secret not in raw
    restored = type(store)(store.path, environ={}).load()
    assert restored.base_url == runtime.provider.base_url
    assert restored.model == runtime.provider.model
    assert restored.api_key == ''


def test_delete_hot_disables_default_without_deleting_encryption_key(tmp_path):
    from tofu_agent import ProviderConfig

    provider = ProviderConfig(
        base_url='http://127.0.0.1:18889/v1',
        api_key='secret', model='mock-model')
    app, runtime, store = _app(tmp_path, provider=provider, source='saved')
    store.save(provider)

    async def scenario():
        response = await app.test_client().delete('/api/v1/setup/provider')
        assert response.status_code == 200
        assert (await response.get_json())['configured'] is False
        assert runtime.provider is None
        assert runtime.default_model == ''

    _run(scenario())
    assert not store.path.exists()
    assert store.key_path.exists()


def test_environment_owned_provider_is_redacted_and_read_only(tmp_path):
    from tofu_agent import ProviderConfig

    secret = 'sk-environment-secret'
    provider = ProviderConfig(
        base_url='http://127.0.0.1:18889/v1',
        api_key=secret, model='env-model')
    app, runtime, store = _app(
        tmp_path, provider=provider, source='environment', editable=False)

    async def scenario():
        client = app.test_client()
        snapshot = await client.get('/api/v1/setup/provider')
        assert snapshot.status_code == 200
        assert secret not in await snapshot.get_data(as_text=True)
        assert (await snapshot.get_json())['editable'] is False

        update = await client.put('/api/v1/setup/provider', json={
            'base_url': provider.base_url,
            'api_key': 'replacement',
            'model': 'replacement-model',
        })
        assert update.status_code == 409
        assert (await update.get_json())['error']['kind'] \
            == 'configuration_locked'
        assert runtime.provider is provider
        assert not store.exists

    _run(scenario())


def test_discovery_falls_back_to_v1_and_classifies_credentials(
    tmp_path, monkeypatch,
):
    from tofu_agent.provider_setup import ProviderSetupService

    app, _runtime, _store = _app(tmp_path)
    calls = []
    responses = [
        _Response(404, {'error': 'missing'}),
        _Response(200, {'data': [
            {'id': 'model-z', 'owned_by': 'mock'},
            {'id': 'model-a', 'owned_by': 'mock'},
            {'id': 'model-a', 'owned_by': 'duplicate'},
        ]}),
    ]

    def request_models(url, draft, _timeout):
        calls.append((url, draft.api_key))
        return responses.pop(0)

    monkeypatch.setattr(
        ProviderSetupService, '_request_models', staticmethod(request_models))

    async def scenario():
        client = app.test_client()
        discovered = await client.post(
            '/api/v1/setup/provider/discover', json={
                'base_url': 'http://127.0.0.1:18889',
                'api_key': 'discovery-secret',
            })
        assert discovered.status_code == 200
        body = await discovered.get_json()
        assert body['base_url'] == 'http://127.0.0.1:18889/v1'
        assert [model['id'] for model in body['models']] \
            == ['model-a', 'model-z']
        assert 'discovery-secret' not in await discovered.get_data(as_text=True)

    _run(scenario())
    assert calls == [
        ('http://127.0.0.1:18889/models', 'discovery-secret'),
        ('http://127.0.0.1:18889/v1/models', 'discovery-secret'),
    ]

    monkeypatch.setattr(
        ProviderSetupService, '_request_models',
        staticmethod(lambda *_args: _Response(401, {'secret': 'echo'})))

    async def unauthorized_scenario():
        denied = await app.test_client().post(
            '/api/v1/setup/provider/discover', json={
                'base_url': 'http://127.0.0.1:18889/v1',
                'api_key': 'never-return-this',
            })
        assert denied.status_code == 502
        body = await denied.get_json()
        assert body['verdict'] == 'unauthorized'
        assert 'never-return-this' not in await denied.get_data(as_text=True)

    _run(unauthorized_scenario())


@pytest.mark.parametrize('response,verdict', [
    (_Response(200, ValueError('not json')), 'invalid_response'),
    (_Response(200, {'object': 'list'}), 'invalid_response'),
    (_Response(200, {'data': []}), 'empty_catalogue'),
    (_Response(429, {}), 'rate_limited'),
    (_Response(503, {}), 'unavailable'),
])
def test_discovery_failure_shapes_are_explicit(
    tmp_path, monkeypatch, response, verdict,
):
    from tofu_agent.provider_setup import ProviderSetupService

    app, _runtime, _store = _app(tmp_path)
    monkeypatch.setattr(
        ProviderSetupService, '_request_models',
        staticmethod(lambda *_args: response))

    async def scenario():
        result = await app.test_client().post(
            '/api/v1/setup/provider/discover', json={
                'base_url': 'http://127.0.0.1:18889/v1',
            })
        assert result.status_code == 502
        assert (await result.get_json())['verdict'] == verdict

    _run(scenario())


def test_real_completion_probe_returns_bounded_secret_free_verdict(
    tmp_path, monkeypatch,
):
    import lib.provider_probe as provider_probe

    app, _runtime, _store = _app(tmp_path)
    captured = {}

    def probe(base_url, api_key, model_id, extra_headers, timeout, **kwargs):
        captured.update({
            'base_url': base_url, 'api_key': api_key, 'model': model_id,
            'headers': extra_headers, 'timeout': timeout, **kwargs,
        })
        return 'unauthorized', f'HTTP 401 upstream echoed {api_key}'

    monkeypatch.setattr(provider_probe, 'probe_one_cell', probe)

    async def scenario():
        secret = 'probe-secret-never-return'
        result = await app.test_client().post(
            '/api/v1/setup/provider/test', json={
                'base_url': 'http://127.0.0.1:18889/v1',
                'api_key': secret,
                'model': 'model-a',
            })
        assert result.status_code == 200
        body = await result.get_json()
        assert body['ok'] is False
        assert body['verdict'] == 'unauthorized'
        assert 'HTTP 401' in body['detail']
        assert secret not in await result.get_data(as_text=True)

    _run(scenario())
    assert captured['protocol'] == 'openai'
    assert captured['model'] == 'model-a'


def test_ssrf_metadata_target_is_rejected_before_save(tmp_path):
    app, runtime, store = _app(tmp_path)

    async def scenario():
        response = await app.test_client().put(
            '/api/v1/setup/provider', json={
                'base_url': 'http://169.254.169.254/latest',
                'api_key': 'secret',
                'model': 'metadata',
            })
        assert response.status_code == 400
        assert 'not an allowed request target' in await response.get_data(
            as_text=True)

    _run(scenario())
    assert runtime.provider is None
    assert not store.exists


@pytest.mark.parametrize('base_url,error', [
    ('http://user:password@127.0.0.1:18889/v1', 'embedded credentials'),
    ('http://127.0.0.1:18889/v1?api_key=secret', 'query or fragment'),
    ('http://127.0.0.1:18889/v1#secret', 'query or fragment'),
])
def test_provider_endpoint_cannot_smuggle_secrets_in_a_public_url(
    base_url, error,
):
    from tofu_agent import AgentConfigurationError, ProviderConfig

    with pytest.raises(AgentConfigurationError, match=error):
        ProviderConfig(base_url=base_url, api_key='', model='model-a')


def test_provider_headers_reject_newline_injection():
    from tofu_agent import AgentConfigurationError, ProviderConfig

    with pytest.raises(AgentConfigurationError, match='newlines'):
        ProviderConfig(
            base_url='http://127.0.0.1:18889/v1',
            api_key='', model='model-a',
            extra_headers={'X-Gateway': 'safe\r\nX-Leak: secret'},
        )


@pytest.mark.parametrize('headers,error', [
    ({'Authorization': 'Bearer shadow-key'}, 'reserved'),
    ({'Host': 'attacker.example'}, 'reserved'),
    ({'Bad Header': 'value'}, 'valid HTTP tokens'),
    ({'X-Gateway': 'value\x00suffix'}, 'control characters'),
])
def test_provider_headers_reject_transport_authority_and_invalid_bytes(
    headers, error,
):
    from tofu_agent import AgentConfigurationError, ProviderConfig

    with pytest.raises(AgentConfigurationError, match=error):
        ProviderConfig(
            base_url='http://127.0.0.1:18889/v1',
            api_key='', model='model-a', extra_headers=headers,
        )


def test_provider_api_key_rejects_header_injection():
    from tofu_agent import AgentConfigurationError, ProviderConfig

    with pytest.raises(AgentConfigurationError, match='control characters'):
        ProviderConfig(
            base_url='http://127.0.0.1:18889/v1',
            api_key='safe\r\nX-Leak: secret', model='model-a',
        )


def test_discovery_rejects_url_credentials_without_echoing_them(tmp_path):
    app, _runtime, _store = _app(tmp_path)

    async def scenario():
        secret = 'password-never-echo'
        response = await app.test_client().post(
            '/api/v1/setup/provider/discover', json={
                'base_url': f'http://user:{secret}@127.0.0.1:18889/v1',
            })
        assert response.status_code == 400
        assert secret not in await response.get_data(as_text=True)

    _run(scenario())


def test_concurrent_saves_keep_runtime_and_encrypted_file_in_one_order(
    tmp_path,
):
    from tofu_agent.provider_setup import ProviderSetupService
    from tofu_agent.provider_store import ProviderSettingsStore

    events = []
    events_lock = threading.Lock()

    class RecordingStore(ProviderSettingsStore):
        def save(self, provider):
            with events_lock:
                events.append(('store', provider.model))
            time.sleep(0.01)
            super().save(provider)

    class RecordingRuntime(_Runtime):
        def configure_provider(self, provider, *, source='runtime'):
            with events_lock:
                events.append(('runtime', provider.model))
            super().configure_provider(provider, source=source)

    store = RecordingStore(tmp_path / 'provider.json', environ={})
    runtime = RecordingRuntime()
    service = ProviderSetupService(runtime, store, editable=True)
    barrier = threading.Barrier(6)

    def save(index):
        barrier.wait()
        return service.save({
            'base_url': f'http://127.0.0.1:{19000 + index}/v1',
            'api_key': f'key-{index}',
            'model': f'model-{index}',
        })

    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(save, range(6)))
    assert all(result['configured'] for result in results)
    assert len(events) == 12
    for offset in range(0, len(events), 2):
        assert events[offset][0] == 'store'
        assert events[offset + 1][0] == 'runtime'
        assert events[offset][1] == events[offset + 1][1]
    restored = store.load()
    assert restored == runtime.provider


def test_setup_can_be_disabled_without_affecting_agent_health(tmp_path):
    app, _runtime, _store = _app(tmp_path, setup_enabled=False)

    async def scenario():
        client = app.test_client()
        page = await client.get('/setup')
        api = await client.get('/api/v1/setup/provider')
        live = await client.get('/health/live')
        assert page.status_code == api.status_code == 404
        assert live.status_code == 200

    _run(scenario())


def test_doctor_accepts_an_unconfigured_fresh_install(
    tmp_path, monkeypatch, capsys,
):
    from tofu_agent.cli import main

    config_path = tmp_path / 'provider.json'
    monkeypatch.setenv('TOFU_AGENT_CONFIG_PATH', str(config_path))
    for name in (
        'TOFU_AGENT_PROVIDER_BASE_URL', 'TOFU_PROVIDER_BASE_URL', 'LLM_BASE_URL',
        'TOFU_AGENT_PROVIDER_API_KEY', 'TOFU_PROVIDER_API_KEY', 'LLM_API_KEY',
        'LLM_API_KEYS', 'TOFU_AGENT_PROVIDER_MODEL', 'TOFU_PROVIDER_MODEL',
        'TOFU_AGENT_MODEL', 'LLM_MODEL',
    ):
        monkeypatch.delenv(name, raising=False)

    assert main(['--env-file', str(tmp_path / 'absent.env'), 'doctor']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['ok'] is True
    assert document['ready'] is False
    assert document['provider'] is None
    assert document['frontend'] is False
    assert document['provider_setup_ui'] is True
    assert document['provider_setup']['editable'] is True
    assert document['provider_setup']['source'] == 'none'


def test_setup_assets_do_not_persist_tokens_or_embed_external_code():
    root = Path(__file__).resolve().parents[1] / 'tofu_agent/setup_ui'
    html = (root / 'index.html').read_text(encoding='utf-8')
    javascript = (root / 'setup.js').read_text(encoding='utf-8')
    stylesheet = (root / 'setup.css').read_text(encoding='utf-8')

    assert 'src="http://' not in html and 'src="https://' not in html
    assert 'href="http://' not in html and 'href="https://' not in html
    assert 'localStorage' not in javascript
    assert 'sessionStorage' not in javascript
    assert 'innerHTML' not in javascript
    assert 'eval(' not in javascript
    assert 'url(http' not in stylesheet
    for control in (
        'templateGrid', 'baseUrl', 'apiKey', 'model', 'discoverButton',
        'testButton', 'saveButton', 'deleteButton', 'statusMessage',
    ):
        assert f'id="{control}"' in html

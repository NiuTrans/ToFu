"""Database-free model-routing v2 setup and encrypted-store contract."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from cryptography.fernet import Fernet
import pytest

from tests.support.standalone_model_routing import (
    standalone_model_routing_envelope,
)


pytestmark = pytest.mark.unit


class _Runtime:
    def __init__(self, model_routing=None, *, source='none'):
        from lib.identity import PrincipalContext

        self.principal = PrincipalContext.user(
            subject_id='setup:test', owner_user_id=41)
        self.model_routing = model_routing
        self.model_routing_source = source
        self.default_model = (
            dict(model_routing.model) if model_routing is not None else None)
        self.closed = False
        self.capacity = 4
        self.in_flight = 0
        self.configurations = []

    def configure_model_routing(self, model_routing, *, source='runtime'):
        self.model_routing = model_routing
        self.model_routing_source = source
        self.default_model = (
            dict(model_routing.model) if model_routing is not None else None)
        self.configurations.append((model_routing, source))

    def get(self, _task_id):
        return None


def _run(coro):
    return asyncio.run(coro)


def _app(
    tmp_path: Path,
    *,
    token: str = '',
    editable: bool = True,
    model_routing=None,
    source: str = 'none',
    setup_enabled: bool = True,
):
    from tofu_agent.provider_setup import ModelRoutingSetupService
    from tofu_agent.provider_store import ModelRoutingSettingsStore
    from tofu_agent.server import HeadlessServerConfig, create_app

    runtime = _Runtime(model_routing, source=source)
    store = ModelRoutingSettingsStore(
        tmp_path / 'model-routing.json', environ={})
    service = ModelRoutingSetupService(
        runtime, store, source=source, editable=editable)
    app = create_app(
        runtime=runtime,
        model_routing_setup=service,
        config=HeadlessServerConfig(
            bind_host='0.0.0.0' if token else '127.0.0.1',
            token=token,
            auth_mode='token' if token else 'open',
            setup_enabled=setup_enabled,
        ),
    )
    app.config['TESTING'] = True
    return app, runtime, store


def test_encrypted_store_round_trip_permissions_redaction_and_delete(tmp_path):
    from tofu_agent import ModelRoutingConfig
    from tofu_agent.provider_store import ModelRoutingSettingsStore

    envelope = standalone_model_routing_envelope(
        secret='sk-store-secret-never-plaintext')
    access = ModelRoutingConfig.from_mapping(envelope)
    store = ModelRoutingSettingsStore(
        tmp_path / 'nested/model-routing.json', environ={})
    store.save(access)

    raw = store.path.read_text(encoding='utf-8')
    assert 'sk-store-secret-never-plaintext' not in raw
    assert 'models.example' in raw
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.key_path.stat().st_mode & 0o777 == 0o600
    assert ModelRoutingSettingsStore(store.path, environ={}).load() == access
    assert 'sk-store-secret-never-plaintext' not in repr(access)
    assert 'sk-store-secret-never-plaintext' not in str(access.public_dict())

    assert store.delete() is True
    assert store.delete() is False
    assert not store.path.exists()
    assert store.key_path.exists()


def test_store_path_honors_explicit_xdg_and_injected_key(tmp_path):
    from tofu_agent import ModelRoutingConfig
    from tofu_agent.provider_store import (
        ModelRoutingSettingsStore,
        default_model_routing_config_path,
    )

    explicit = tmp_path / 'explicit/model-routing.json'
    assert default_model_routing_config_path({
        'TOFU_AGENT_CONFIG_PATH': str(explicit),
        'XDG_CONFIG_HOME': str(tmp_path / 'ignored'),
    }) == explicit
    assert default_model_routing_config_path({
        'XDG_CONFIG_HOME': str(tmp_path / 'xdg'),
    }) == tmp_path / 'xdg/tofu-agent/model-routing.json'

    key = Fernet.generate_key().decode('ascii')
    store = ModelRoutingSettingsStore(
        explicit, environ={'TOFU_AGENT_CONFIG_KEY': key})
    store.save(ModelRoutingConfig.from_mapping(
        standalone_model_routing_envelope()))
    assert store.path.exists()
    assert not store.key_path.exists()
    assert store.load() is not None


def test_store_fails_closed_for_missing_key_and_corrupt_document(tmp_path):
    from tofu_agent import ModelRoutingConfig
    from tofu_agent.provider_store import (
        ModelRoutingSettingsStore,
        ModelRoutingStoreError,
    )

    store = ModelRoutingSettingsStore(
        tmp_path / 'model-routing.json', environ={})
    store.save(ModelRoutingConfig.from_mapping(
        standalone_model_routing_envelope()))
    store.key_path.unlink()
    with pytest.raises(ModelRoutingStoreError, match='key is missing'):
        store.load()

    store.path.write_text('{broken', encoding='utf-8')
    with pytest.raises(ModelRoutingStoreError, match='unreadable|invalid'):
        store.load()


def test_setup_page_public_but_configuration_requires_token(tmp_path):
    app, _runtime, _store = _app(tmp_path, token='sidecar-secret')

    async def scenario():
        client = app.test_client()
        assert (await client.get('/setup')).status_code == 200
        denied = await client.get('/api/v1/setup/model-routing')
        assert denied.status_code == 401
        allowed = await client.get(
            '/api/v1/setup/model-routing',
            headers={'Authorization': 'Bearer sidecar-secret'})
        assert allowed.status_code == 200
        body = await allowed.get_json()
        assert body['configured'] is False
        assert body['storage']['contract_version'] == 'tofu.model-routing/v2'

    _run(scenario())


def test_save_is_secret_free_hot_applied_and_restartable(tmp_path):
    app, runtime, store = _app(tmp_path)
    envelope = standalone_model_routing_envelope(
        secret='sk-save-never-return')

    async def scenario():
        client = app.test_client()
        response = await client.put(
            '/api/v1/setup/model-routing', json=envelope)
        assert response.status_code == 200
        encoded = await response.get_data(as_text=True)
        assert 'sk-save-never-return' not in encoded
        body = await response.get_json()
        assert body['configured'] is True
        assert body['ready'] is True
        assert body['source'] == 'saved'
        assert body['model_routing']['credential_secret_hints'] == {
            'provider-a-secret': 'configured',
        }

    _run(scenario())
    assert runtime.default_model == {
        'creator_id': 'test-creator', 'model_id': 'model-a'}
    restored = store.load()
    assert restored is not None
    assert restored.credential_secrets == {
        'provider-a-secret': 'sk-save-never-return'}
    assert runtime.configurations[-1][1] == 'saved'


def test_delete_hot_disables_default_but_retains_encryption_key(tmp_path):
    from tofu_agent import ModelRoutingConfig

    access = ModelRoutingConfig.from_mapping(
        standalone_model_routing_envelope())
    app, runtime, store = _app(
        tmp_path, model_routing=access, source='saved')
    store.save(access)

    async def scenario():
        response = await app.test_client().delete(
            '/api/v1/setup/model-routing')
        assert response.status_code == 200
        assert (await response.get_json())['configured'] is False

    _run(scenario())
    assert runtime.model_routing is None
    assert runtime.default_model is None
    assert not store.path.exists()
    assert store.key_path.exists()


def test_environment_owned_configuration_is_redacted_and_locked(tmp_path):
    from tofu_agent import ModelRoutingConfig

    access = ModelRoutingConfig.from_mapping(
        standalone_model_routing_envelope(secret='sk-environment-secret'))
    app, _runtime, _store = _app(
        tmp_path, model_routing=access, source='environment', editable=False)

    async def scenario():
        client = app.test_client()
        read = await client.get('/api/v1/setup/model-routing')
        assert read.status_code == 200
        text = await read.get_data(as_text=True)
        assert 'sk-environment-secret' not in text
        assert (await read.get_json())['editable'] is False
        locked = await client.put(
            '/api/v1/setup/model-routing',
            json=standalone_model_routing_envelope())
        assert locked.status_code == 409
        assert (await locked.get_json())['error']['kind'] == 'configuration_locked'

    _run(scenario())


def test_probe_uses_computed_deployment_without_persisting(
    tmp_path, monkeypatch,
):
    app, runtime, store = _app(tmp_path)
    captured = {}

    monkeypatch.setattr(
        'lib.byo_egress.validate_egress_url', lambda url: captured.update(url=url))
    monkeypatch.setattr(
        'lib.provider_probe.probe_one_cell',
        lambda base_url, api_key, model, headers, timeout_s, protocol: (
            captured.update(
                base_url=base_url,
                api_key=api_key,
                model=model,
                protocol=protocol,
            ) or ('ok', 'ready')
        ),
    )

    async def scenario():
        response = await app.test_client().post(
            '/api/v1/setup/model-routing/test',
            json=standalone_model_routing_envelope(secret='sk-probe'))
        assert response.status_code == 200
        body = await response.get_json()
        assert body['ok'] is True
        assert body['provider_id'] == 'provider-a'
        assert body['deployment_id'] == 'provider-a-deployment'

    _run(scenario())
    assert captured['api_key'] == 'sk-probe'
    assert captured['model'] == 'wire/model-a'
    assert captured['protocol'] == 'openai'
    assert runtime.model_routing is None
    assert not store.path.exists()


def test_setup_rejects_cross_site_mutation(tmp_path):
    app, _runtime, _store = _app(tmp_path)

    async def scenario():
        response = await app.test_client().put(
            '/api/v1/setup/model-routing',
            headers={'Origin': 'https://attacker.invalid'},
            json=standalone_model_routing_envelope())
        assert response.status_code == 403
        assert (await response.get_json())['error']['kind'] == 'cross_site_request'

    _run(scenario())


def test_setup_can_be_disabled_without_affecting_liveness(tmp_path):
    app, _runtime, _store = _app(tmp_path, setup_enabled=False)

    async def scenario():
        client = app.test_client()
        assert (await client.get('/setup')).status_code == 404
        assert (await client.get('/api/v1/setup/model-routing')).status_code == 404
        assert (await client.get('/health/live')).status_code == 200

    _run(scenario())


def test_doctor_accepts_unconfigured_install(tmp_path, monkeypatch, capsys):
    from tofu_agent.cli import main

    monkeypatch.delenv('TOFU_AGENT_MODEL_ROUTING', raising=False)
    monkeypatch.setenv(
        'TOFU_AGENT_CONFIG_PATH', str(tmp_path / 'missing.json'))
    assert main(['doctor']) == 0
    document = json.loads(capsys.readouterr().out)
    assert document['ready'] is False
    assert document['model'] is None
    assert document['model_routing'] is None
    assert document['model_routing_setup']['editable'] is True


def test_setup_assets_do_not_persist_tokens_or_embed_external_code():
    root = Path(__file__).parents[1] / 'tofu_agent/setup_ui'
    html = (root / 'index.html').read_text(encoding='utf-8')
    script = (root / 'setup.js').read_text(encoding='utf-8')
    for control in (
        'routingForm', 'routingJson', 'testButton', 'saveButton',
        'deleteButton',
    ):
        assert f'id="{control}"' in html
    assert 'localStorage' not in script
    assert 'sessionStorage' not in script
    assert 'http://' not in html + script
    assert 'https://' not in html + script

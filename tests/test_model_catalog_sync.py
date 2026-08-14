"""Remote provider model-catalogue reconciliation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lib.llm_dispatch import model_catalog_sync as catalog

pytestmark = pytest.mark.unit


def _model(model_id, **extra):
    row = {
        'model_id': model_id,
        'aliases': [],
        'capabilities': ['text'],
        'rpm': 30,
        'cost': 0.01,
        'thinking_default': False,
    }
    row.update(extra)
    return row


def _write_config(path, provider, **extra):
    cfg = {'providers': [provider]}
    cfg.update(extra)
    path.write_text(json.dumps(cfg), encoding='utf-8')


def _read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def test_reconcile_adds_new_and_retires_only_after_consecutive_snapshots():
    first = catalog.reconcile_catalog_models(
        [_model('retired')], [_model('new')], {}, remove_after=2)
    assert [m['model_id'] for m in first['models']] == ['retired', 'new']
    assert first['added'] == ['new']
    assert first['removed'] == []
    assert first['pending_removals'] == {'retired': 1}
    assert first['models'][1]['catalog_managed'] is True

    second = catalog.reconcile_catalog_models(
        first['models'], [_model('new')], first['pending_removals'],
        remove_after=2)
    assert [m['model_id'] for m in second['models']] == ['new']
    assert second['added'] == []
    assert second['removed'] == ['retired']
    assert second['pending_removals'] == {}


def test_logical_request_pool_matches_live_wire_ids_without_duplicates():
    logical = _model('claude-opus', request_ids=['gateway-opus-a'])
    result = catalog.reconcile_catalog_models(
        [logical], [_model('gateway-opus-a')], {}, remove_after=2)
    assert [m['model_id'] for m in result['models']] == ['claude-opus']
    assert result['added'] == [] and result['removed'] == []


def test_hand_pinned_model_is_never_retired():
    pinned = _model('private-deployment', catalog_pinned=True)
    result = catalog.reconcile_catalog_models(
        [pinned], [_model('public-model')],
        {'private-deployment': 99}, remove_after=2)
    assert {m['model_id'] for m in result['models']} == {
        'private-deployment', 'public-model'}
    assert result['removed'] == []
    assert 'private-deployment' not in result['pending_removals']


def test_sync_once_is_default_auto_and_repairs_retired_references(
        tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_MODEL_CATALOG_SYNC', raising=False)
    path = tmp_path / 'server_config.json'
    provider = {
        'id': 'official', 'name': 'Official', 'brand': 'openai',
        'base_url': 'https://api.example.com/v1', 'api_keys': ['secret'],
        'enabled': True, 'models': [_model('retired')],
        # No model_catalog_sync field: legacy configs migrate to auto.
    }
    _write_config(
        path, provider,
        presets={'opus': 'retired', 'qwen': 'retired'},
        model_defaults={
            'default_model': 'retired', 'fallback_model': 'retired'},
        models={
            'LLM_MODEL': 'retired',
            'QWEN_MODEL': 'retired',
            'EMBEDDING_MODELS': ['retired', 'embedding-current'],
        },
    )
    rebuilds = []
    discover = lambda _provider: [_model('current')]

    first = catalog.sync_once(
        force=True, discover=discover, now=100, remove_after=2,
        config_path=str(path), rebuild=lambda: rebuilds.append('rebuild'))
    assert first['added'] == ['current'] and first['removed'] == []
    cfg = _read(path)
    state = cfg['providers'][0]['model_catalog_sync']
    assert state['mode'] == 'auto'
    assert state['pending_removals'] == {'retired': 1}
    assert state['last_error'] == ''

    second = catalog.sync_once(
        force=True, discover=discover, now=200, remove_after=2,
        config_path=str(path), rebuild=lambda: rebuilds.append('rebuild'))
    assert second['removed'] == ['retired']
    cfg = _read(path)
    assert [m['model_id'] for m in cfg['providers'][0]['models']] == ['current']
    assert cfg['presets'] == {'opus': 'current'}
    assert cfg['model_defaults'] == {
        'default_model': 'current', 'fallback_model': ''}
    assert cfg['models'] == {
        'LLM_MODEL': 'current',
        'EMBEDDING_MODELS': ['embedding-current'],
    }
    assert rebuilds == ['rebuild', 'rebuild']


def test_failed_or_empty_fetch_keeps_last_good_models(tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_MODEL_CATALOG_SYNC', raising=False)
    path = tmp_path / 'server_config.json'
    provider = {
        'id': 'relay', 'name': 'Relay', 'base_url': 'https://relay.example/v1',
        'api_keys': ['secret'], 'enabled': True,
        'models': [_model('last-good')],
        'model_catalog_sync': {'mode': 'auto'},
    }
    _write_config(path, provider)

    stats = catalog.sync_once(
        force=True, discover=lambda _provider: [], now=123,
        config_path=str(path), rebuild=lambda: pytest.fail('must not rebuild'))
    cfg = _read(path)
    assert stats['failed'] == 1 and stats['changed'] == 0
    assert [m['model_id'] for m in cfg['providers'][0]['models']] == ['last-good']
    state = cfg['providers'][0]['model_catalog_sync']
    assert state['consecutive_failures'] == 1
    assert 'keeping last-good catalogue' in state['last_error']
    assert state.get('pending_removals') in (None, {})


def test_changed_catalog_reloads_defaults_before_dispatcher_reset(
        tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_MODEL_CATALOG_SYNC', raising=False)
    path = tmp_path / 'server_config.json'
    _write_config(path, {
        'id': 'remote', 'base_url': 'https://example.test/v1',
        'api_keys': ['secret'], 'enabled': True, 'models': [],
        'model_catalog_sync': {'mode': 'auto'},
    })
    import lib
    import lib.llm_dispatch
    calls = []
    monkeypatch.setattr(lib, 'reload_config', lambda: calls.append('reload'))
    monkeypatch.setattr(
        lib.llm_dispatch, 'reset_dispatcher', lambda: calls.append('reset'))
    stats = catalog.sync_once(
        force=True, discover=lambda _provider: [_model('new')], now=123,
        config_path=str(path))
    assert stats['changed'] == 1
    assert calls == ['reload', 'reset']


def test_manual_mode_and_local_or_managed_catalogues_are_skipped(
        tmp_path, monkeypatch):
    monkeypatch.delenv('TOFU_MODEL_CATALOG_SYNC', raising=False)
    path = tmp_path / 'server_config.json'
    providers = [
        {'id': 'manual', 'base_url': 'https://a.example/v1',
         'api_keys': ['x'], 'enabled': True, 'models': [_model('a')],
         'model_catalog_sync': {'mode': 'manual'}},
        {'id': 'local', 'brand': 'local', 'base_url': 'http://127.0.0.1:8000/v1',
         'api_keys': ['x'], 'enabled': True, 'models': [_model('b')]},
        {'id': 'oauth', 'oauth': 'codex', 'base_url': 'https://c.example/v1',
         'api_keys': ['x'], 'enabled': True, 'models': [_model('c')]},
    ]
    path.write_text(json.dumps({'providers': providers}), encoding='utf-8')
    stats = catalog.sync_once(
        force=True,
        discover=lambda _provider: pytest.fail('no provider should be polled'),
        config_path=str(path))
    assert stats['providers'] == 0


def test_frontend_onboarding_enables_sync_and_manual_rows_are_pinned():
    root = Path(catalog.__file__).resolve().parents[2]
    runtime = (root / 'frontend/src/runtime/app-runtime.js').read_text(
        encoding='utf-8')
    # The Vite migration concatenates onboarding, auto-setup, and template
    # actions into the runtime module; keep asserting all three creation seams.
    assert runtime.count("model_catalog_sync: { mode: 'auto' }") >= 3
    assert 'catalog_pinned: true' in runtime
    assert '_offerTemplateUpdate(tpl.key, p.models)' not in runtime
    assert 'data.model_catalog_sync_started' in runtime


def test_stale_settings_snapshot_preserves_new_live_models_and_retirements():
    from routes.config import _merge_server_owned_providers

    existing = [{
        'id': 'remote',
        'base_url': 'https://example.test/v1',
        'models': [_model('kept'), _model('new-live')],
        'model_catalog_sync': {
            'mode': 'auto', 'last_success_at': 200, 'catalog_size': 2},
    }]
    stale_editor = [{
        'id': 'remote',
        'base_url': 'https://example.test/v1',
        'models': [
            _model('kept', rpm=99),
            _model('already-retired', catalog_managed=True),
            _model('private', catalog_pinned=True),
        ],
        'model_catalog_sync': {'mode': 'auto', 'last_success_at': 100},
    }]
    provider = _merge_server_owned_providers(existing, stale_editor)[0]
    assert [m['model_id'] for m in provider['models']] == [
        'kept', 'private', 'new-live']
    assert provider['models'][0]['rpm'] == 99
    assert provider['model_catalog_sync']['last_success_at'] == 200


def test_switching_catalog_to_manual_fences_inflight_claim():
    from routes.config import _merge_server_owned_providers

    existing = [{
        'id': 'remote', 'models': [],
        'model_catalog_sync': {
            'mode': 'auto', 'claim_token': 'worker-token',
            'lease_until': 999, 'last_success_at': 100},
    }]
    incoming = [{
        'id': 'remote', 'models': [],
        'model_catalog_sync': {'mode': 'manual', 'last_success_at': 100},
    }]
    state = _merge_server_owned_providers(existing, incoming)[0][
        'model_catalog_sync']
    assert state['mode'] == 'manual'
    assert state['lease_until'] == 0
    assert 'claim_token' not in state


def test_changing_provider_connection_resets_old_catalog_state():
    from routes.config import _merge_server_owned_providers

    existing = [{
        'id': 'remote', 'base_url': 'https://old.example/v1',
        'api_keys': ['old-key'], 'models': [_model('old-live')],
        'model_catalog_sync': {
            'mode': 'auto', 'claim_token': 'old-claim', 'lease_until': 999,
            'last_success_at': 200, 'pending_removals': {'old-live': 1}},
    }]
    incoming = [{
        'id': 'remote', 'base_url': 'https://new.example/v1',
        'api_keys': ['new-key'], 'models': [_model('new-template')],
        'model_catalog_sync': {'mode': 'auto', 'last_success_at': 100},
    }]
    provider = _merge_server_owned_providers(existing, incoming)[0]
    assert [m['model_id'] for m in provider['models']] == ['new-template']
    state = provider['model_catalog_sync']
    assert state == {'mode': 'auto', 'lease_until': 0}

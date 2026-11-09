"""Host search-gateway settings (lib/search/search_gateway.py + server-config route + bridge).

Pins the credential and persistence contract:
  * catalog files layer bundles first, user drop-ins last; bad files are skipped;
  * a Settings payload is validated before any side effect, and its plaintext
    key is split off for the credentials vault — never persisted or echoed;
  * a payload without the gateway section keeps the saved gateway;
  * the engine projection is inactive unless enabled, keyed and sourced, and a
    disabled gateway still clears every tofu-search gateway field.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import lib.search.search_gateway as sg
from lib.credentials_vault import delete_entry, get_entry, set_entry

pytestmark = pytest.mark.unit

ENDPOINT = 'https://gateway.example.test/search'
SECRET = 'billing-key-0123456789'


def _write(directory, name, payload):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding='utf-8')


def _description(gateway_id='alpha', endpoint=ENDPOINT, **extra):
    entry = {'id': gateway_id, 'name': 'Alpha', 'endpoint': endpoint,
             'sources': [{'id': 'one-search', 'name': 'One', 'default': True},
                         {'id': 'two-search', 'name': 'Two'}]}
    entry.update(extra)
    return entry


@pytest.fixture
def catalog_dirs(tmp_path, monkeypatch):
    bundles = tmp_path / 'bundles'
    user = tmp_path / 'config'
    monkeypatch.setattr(sg, '_BUNDLES_ROOT', bundles)
    monkeypatch.setattr(sg, 'config_path', lambda *parts: str(user.joinpath(*parts)))
    _write(bundles / 'acme' / 'search_gateways', 'alpha.json', _description())
    return bundles, user


@pytest.fixture
def vault_key():
    delete_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY)
    yield
    delete_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY)


def test_catalog_layers_bundles_then_user_dropins(catalog_dirs):
    _, user = catalog_dirs
    _write(user / 'search_gateways', 'alpha.json',
           _description(endpoint='https://override.example.test/search'))
    _write(user / 'search_gateways', 'broken.json', '{not json')
    _write(user / 'search_gateways', 'leaky.json',
           _description('leaky', endpoint='https://user:pw@gateway.example.test/'))
    catalog = sg.load_gateway_catalog()
    assert sorted(catalog) == ['alpha']
    assert catalog['alpha']['endpoint'] == 'https://override.example.test/search'
    assert [s['id'] for s in catalog['alpha']['sources']] == ['one-search', 'two-search']
    assert sg.catalog_payload() == [catalog['alpha']]


def test_update_splits_the_key_from_the_persisted_shape(catalog_dirs):
    persisted, key = sg.prepare_gateway_update({
        'enabled': True, 'id': 'alpha', 'free_engines': 'off', 'full_content': True,
        'sources': ['one-search', 'one-search', ' two-search '], 'api_key': f' {SECRET} ',
    })
    assert key == SECRET
    assert persisted == {
        'enabled': True, 'id': 'alpha', 'sources': ['one-search', 'two-search'],
        'full_content': True, 'free_engines': 'off',
        'credential_vault': sg.SEARCH_GATEWAY_VAULT_ENTRY,
    }


def test_a_custom_gateway_persists_its_endpoint(catalog_dirs):
    persisted, key = sg.prepare_gateway_update(
        {'enabled': True, 'endpoint': ENDPOINT, 'sources': ['one-search']})
    assert persisted['endpoint'] == ENDPOINT and persisted['id'] == '' and key == ''


@pytest.mark.parametrize('payload', [
    {'free_engines': 'sometimes'},
    {'sources': 'one-search'},
    {'sources': ['has space']},
    {'enabled': 'yes'},
    {'enabled': True, 'endpoint': 'https://user:pw@gateway.example.test/'},
    {'api_key': 12345},
    {'api_key': 'x' * 8193},
    'not-an-object',
])
def test_malformed_updates_are_rejected(catalog_dirs, payload):
    with pytest.raises(ValueError):
        sg.prepare_gateway_update(payload)


def test_an_uncatalogued_gateway_without_endpoint_is_kept_but_inactive(catalog_dirs, vault_key):
    persisted, _key = sg.prepare_gateway_update(
        {'enabled': True, 'id': 'elsewhere', 'sources': ['one-search']})
    assert persisted['enabled'] is True and 'endpoint' not in persisted
    set_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY, SECRET)
    assert sg.engine_settings({'gateway': persisted})['gateway_url'] == ''


def test_projection_reports_key_presence_never_the_key(catalog_dirs, vault_key):
    saved = {'gateway': {'enabled': True, 'id': 'alpha', 'sources': ['one-search'],
                         'free_engines': 'parallel'}}
    assert sg.public_gateway_settings(saved)['has_key'] is False
    set_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY, SECRET)
    public = sg.public_gateway_settings(saved)
    assert public['has_key'] is True and public['key_hint']
    assert public['free_engines'] == 'parallel' and public['sources'] == ['one-search']
    assert SECRET not in json.dumps(public)


def test_engine_projection_requires_enabled_keyed_sourced_gateway(catalog_dirs, vault_key):
    saved = {'gateway': {'enabled': True, 'id': 'alpha', 'sources': ['one-search'],
                         'free_engines': 'bogus', 'full_content': True}}
    inactive = sg.engine_settings(saved)
    assert inactive['gateway_url'] == '' and inactive['gateway_sources'] == []
    assert inactive['free_engines'] == 'fallback' and inactive['gateway_full_content'] is True

    set_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY, SECRET)
    active = sg.engine_settings(saved)
    assert active['gateway_url'] == ENDPOINT
    assert active['gateway_api_key'] == SECRET
    assert active['gateway_sources'] == ['one-search']

    saved['gateway']['enabled'] = False
    assert sg.engine_settings(saved)['gateway_url'] == ''
    assert sg.engine_settings({})['gateway_url'] == ''


# ── server-config route ──

@pytest.fixture
def config_file(tmp_path, monkeypatch):
    import routes.config as rc

    path = tmp_path / 'server_config.json'
    monkeypatch.setattr(rc, '_SERVER_CONFIG_PATH', str(path))
    return path


def test_route_stores_key_in_vault_and_never_returns_it(
        flask_client, catalog_dirs, vault_key, config_file):
    response = flask_client.post('/api/v4/server-config', json={'search': {
        'profile': 'balanced',
        'gateway': {'enabled': True, 'id': 'alpha', 'sources': ['one-search'],
                    'free_engines': 'fallback', 'full_content': False, 'api_key': SECRET},
    }})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert get_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY) == SECRET
    text = config_file.read_text()
    assert SECRET not in text
    assert json.loads(text)['search']['gateway']['credential_vault'] == sg.SEARCH_GATEWAY_VAULT_ENTRY

    payload = flask_client.get('/api/v4/server-config').get_data(as_text=True)
    assert SECRET not in payload
    search = json.loads(payload)
    search = search.get('data', search)['search']
    assert search['gateway']['has_key'] is True
    assert search['gateway']['sources'] == ['one-search']
    assert [g['id'] for g in search['gateway_catalog']] == ['alpha']
    # Search limits are fixed defaults: no tuning knob is projected.
    assert set(search) == {'skip_domains', 'gateway', 'gateway_catalog'}


def test_route_never_persists_the_catalog_projection(
        flask_client, catalog_dirs, vault_key, config_file):
    response = flask_client.post('/api/v4/server-config', json={'search': {
        'profile': 'balanced', 'gateway_catalog': [{'id': 'stale'}]}})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert 'gateway_catalog' not in json.loads(config_file.read_text())['search']


def test_route_keeps_saved_gateway_when_payload_omits_it(
        flask_client, catalog_dirs, vault_key, config_file):
    saved_gateway = {'enabled': True, 'id': 'alpha', 'sources': ['two-search'],
                     'full_content': False, 'free_engines': 'off',
                     'credential_vault': sg.SEARCH_GATEWAY_VAULT_ENTRY}
    config_file.write_text(json.dumps({'search': {'gateway': saved_gateway}}))
    response = flask_client.post('/api/v4/server-config', json={'search': {
        'skip_domains': ['spam.example'], 'profile': 'fast', 'fetch_top_n': 9,
        'llm_content_filter': True}})
    assert response.status_code == 200, response.get_data(as_text=True)
    saved = json.loads(config_file.read_text())['search']
    # Retired tuning keys from an older client are dropped, not persisted.
    assert saved == {'skip_domains': ['spam.example'], 'skip_domains_schema': 2,
                     'gateway': saved_gateway}


def test_route_keeps_optimizer_fetch_skips_and_stamps_bans(
        flask_client, catalog_dirs, vault_key, config_file):
    """Settings edits operator bans only: the Daily Optimizer's fetch-quality
    skips survive a save untouched, whatever the client sends."""
    config_file.write_text(json.dumps({'search': {
        'skip_domains': ['tiktok.com', 'youtube.com', 'spam.example'],
        'fetch_skip_domains': ['junk.example']}}))
    response = flask_client.post('/api/v4/server-config', json={'search': {
        'skip_domains': ['spam.example'], 'fetch_skip_domains': ['client.example']}})
    assert response.status_code == 200, response.get_data(as_text=True)
    saved = json.loads(config_file.read_text())['search']
    assert saved == {'skip_domains': ['spam.example'], 'skip_domains_schema': 2,
                     'fetch_skip_domains': ['junk.example']}


def test_route_rejects_invalid_gateway_before_any_vault_write(
        flask_client, catalog_dirs, vault_key, config_file):
    response = flask_client.post('/api/v4/server-config', json={'search': {
        'gateway': {'enabled': True, 'free_engines': 'sometimes', 'api_key': SECRET}}})
    assert response.status_code == 400
    assert get_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY) is None
    assert not config_file.exists()


# ── bridge sync ──

def _synced_kwargs(monkeypatch, saved_search):
    import lib.search.search_bridge as sb

    monkeypatch.setattr(sb._lib, '_SAVED_CONFIG', {'search': saved_search}, raising=False)
    monkeypatch.setattr(sb, '_resolve_proxy_url', lambda: '')
    captured = {}
    with patch('tofu_search.configure', side_effect=lambda **kw: captured.update(kw)):
        sb.sync_search_config()
    return captured


def test_bridge_forwards_an_active_gateway_and_clears_a_disabled_one(
        monkeypatch, catalog_dirs, vault_key):
    set_entry(sg.SEARCH_GATEWAY_VAULT_ENTRY, SECRET)
    gateway = {'enabled': True, 'id': 'alpha', 'sources': ['one-search', 'two-search'],
               'free_engines': 'parallel', 'full_content': True}
    active = _synced_kwargs(monkeypatch, {'gateway': gateway})
    assert active['gateway_url'] == ENDPOINT and active['gateway_api_key'] == SECRET
    assert active['gateway_sources'] == ['one-search', 'two-search']
    assert active['free_engines'] == 'parallel' and active['gateway_full_content'] is True

    disabled = _synced_kwargs(monkeypatch, {'gateway': {**gateway, 'enabled': False}})
    assert disabled['gateway_url'] == '' and disabled['gateway_api_key'] == ''
    assert disabled['gateway_sources'] == []

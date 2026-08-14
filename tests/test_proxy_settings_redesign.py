"""Contracts for the Network settings proxy/bypass redesign."""

from __future__ import annotations

import json

import pytest

import lib.proxy as proxy

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_bypass_state():
    saved = list(proxy._settings_domains)
    yield
    proxy.set_bypass_domains(saved)


def test_structured_auth_is_optional_and_valid_for_global_proxy():
    entries, creds, err = proxy.sanitize_proxy_pool([{
        'id': 'office',
        'url': 'https://proxy.example.com:8443',
        'scope': 'global',
        'username': 'alice',
        'password': '  secret:with-colons  ',
    }])

    assert err == ''
    assert entries == [{
        'id': 'office',
        'name': '',
        'url': 'https://proxy.example.com:8443',
        'scope': 'global',
        'enabled': True,
        'credential_vault': 'proxy_office_auth',
    }]
    assert creds == {'office': 'alice:  secret:with-colons  '}


def test_clear_credential_removes_existing_vault_reference():
    entries, creds, err = proxy.sanitize_proxy_pool([{
        'id': 'office',
        'url': 'http://proxy.example.com:8080',
        'scope': 'global',
        'credential_vault': 'proxy_office_auth',
        'clear_credential': True,
    }])

    assert err == ''
    assert creds == {}
    assert 'credential_vault' not in entries[0]


def test_bypass_domains_normalize_urls_wildcards_ports_idna_and_duplicates():
    values, err = proxy.sanitize_bypass_domains([
        ' HTTPS://API.Internal.Example.com:8443/v1?q=1 ',
        '*.Corp.Example.com',
        '.corp.example.com',
        'gateway.local:8080',
        '例子.测试',
        '[2001:db8::1]',
        '',
    ])

    assert err == ''
    assert values == [
        'api.internal.example.com',
        '.corp.example.com',
        'gateway.local',
        'xn--fsqu00a.xn--0zwm56d',
        '2001:db8::1',
    ]


@pytest.mark.parametrize('value', [
    'not a host',
    'internal.example.com/path-without-scheme',
    '*.127.0.0.1',
    'http://:8080',
])
def test_bypass_domains_reject_ambiguous_or_invalid_values(value):
    values, err = proxy.sanitize_bypass_domains([value])
    assert values is None
    assert err


def test_bypass_matching_distinguishes_exact_host_from_suffix():
    assert proxy._host_matches_bypass(
        'api.internal.example.com', ('api.internal.example.com',))
    assert not proxy._host_matches_bypass(
        'evilapi.internal.example.com', ('api.internal.example.com',))
    assert proxy._host_matches_bypass(
        'internal.example.com', ('.internal.example.com',))
    assert proxy._host_matches_bypass(
        'api.internal.example.com', ('.internal.example.com',))
    assert not proxy._host_matches_bypass(
        'notinternal.example.com', ('.internal.example.com',))


def test_save_bypass_list_persists_canonical_values(flask_client, tmp_path, monkeypatch):
    import routes.config as rc

    config_path = tmp_path / 'server_config.json'
    monkeypatch.setattr(rc, '_SERVER_CONFIG_PATH', str(config_path))
    response = flask_client.post('/api/v1/server-config', json={
        'proxy_bypass_domains': [
            'HTTPS://API.Internal.Example.com:8443/v1',
            '*.Corp.Example.com',
            '.corp.example.com',
        ],
    })

    assert response.status_code == 200, response.get_data(as_text=True)
    saved = json.loads(config_path.read_text())
    assert saved['proxy_bypass_domains'] == [
        'api.internal.example.com', '.corp.example.com']


def test_save_bypass_list_rejects_non_array(flask_client):
    response = flask_client.post('/api/v1/server-config', json={
        'proxy_bypass_domains': '.internal.example.com',
    })
    assert response.status_code == 400
    assert response.get_json()['ok'] is False


def test_invalid_bypass_is_rejected_before_proxy_secret_side_effect(flask_client):
    from lib.credentials_vault import delete_entry, get_entry

    vault_name = 'proxy_validation-guard_auth'
    delete_entry(vault_name)
    response = flask_client.post('/api/v1/server-config', json={
        'proxy_bypass_domains': ['not a valid host'],
        'proxy_pool': [{
            'id': 'validation-guard',
            'url': 'http://proxy.example.com:8080',
            'scope': 'global',
            'username': 'alice',
            'password': 'must-not-be-written',
        }],
    })

    assert response.status_code == 400
    assert get_entry(vault_name) is None

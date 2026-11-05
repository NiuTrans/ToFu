"""MCP env may reference a credential-vault entry instead of plaintext.

This is what makes a carried MCP registration portable: the export writes
``{"GITHUB_PERSONAL_ACCESS_TOKEN": "vault:github_token"}`` instead of an
absolute interpreter wrapper that reads the vault, and each destination
resolves the reference against its own vault at launch.
"""
from __future__ import annotations

import pytest

from lib.mcp.stdio_environment import (
    build_stdio_environment,
    resolve_vault_env_references,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def vault(monkeypatch, tmp_path):
    import lib.credentials_vault as credentials_vault

    monkeypatch.setattr(credentials_vault, '_STORE_PATH', tmp_path / 'vault.json')
    monkeypatch.setattr(credentials_vault, '_KEY_PATH', tmp_path / '.vault.key')
    monkeypatch.setattr(credentials_vault, '_fernet', None)
    credentials_vault.set_entry('github_token', 'secret-token-value')
    return credentials_vault


def test_plaintext_values_pass_through_unchanged():
    overlay = {'A': 'literal', 'B': 'not-a-reference:vault'}
    assert resolve_vault_env_references(overlay) is overlay


def test_a_reference_resolves_against_the_vault(vault):
    resolved = resolve_vault_env_references({
        'GITHUB_PERSONAL_ACCESS_TOKEN': 'vault:github_token',
        'UNCHANGED': 'literal',
    })
    assert resolved['GITHUB_PERSONAL_ACCESS_TOKEN'] == 'secret-token-value'
    assert resolved['UNCHANGED'] == 'literal'
    assert 'vault:github_token' not in resolved.values()


def test_a_missing_entry_fails_closed_instead_of_launching_without_credentials(vault):
    with pytest.raises(ValueError, match='not.*configured|not configured'):
        resolve_vault_env_references({'TOKEN': 'vault:absent_entry'})


def test_an_invalid_entry_name_is_rejected(vault):
    with pytest.raises(ValueError, match='invalid credential vault entry'):
        resolve_vault_env_references({'TOKEN': 'vault:../../etc/passwd'})


def test_build_stdio_environment_resolves_references(vault, monkeypatch):
    monkeypatch.setenv('HOME', '/tmp')
    environment = build_stdio_environment(
        {'command': 'npx', 'env': {'GITHUB_PERSONAL_ACCESS_TOKEN': 'vault:github_token'}},
        prepare_directories=False)
    assert environment['GITHUB_PERSONAL_ACCESS_TOKEN'] == 'secret-token-value'

"""First-run recovery-token behavior over the credential authority."""

from __future__ import annotations

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_personal_credentials(monkeypatch, tmp_path):
    from lib import api_keys
    from lib.api_keys import list_keys, revoke_key

    token_file = tmp_path / '.first_run_token'
    monkeypatch.setattr(api_keys, '_FIRST_RUN_TOKEN_FILE', str(token_file))
    for row in list_keys(owner_user_id=1):
        revoke_key(row['id'], owner_user_id=1)
    yield token_file
    for row in list_keys(owner_user_id=1):
        revoke_key(row['id'], owner_user_id=1)


def test_bootstrap_writes_a_valid_admin_recovery_token(
    isolated_personal_credentials,
):
    from lib.api_keys import bootstrap_personal_key, validate_token

    plaintext = bootstrap_personal_key()

    assert plaintext is not None
    assert isolated_personal_credentials.read_text().strip() == plaintext
    context = validate_token(plaintext)
    assert context is not None and context.has_scope('admin')


def test_bootstrap_is_atomic_and_does_not_mint_twice():
    from lib.api_keys import bootstrap_personal_key, list_keys

    assert bootstrap_personal_key() is not None
    assert bootstrap_personal_key() is None
    assert len(list_keys(owner_user_id=1)) == 1


def test_revoking_bootstrap_removes_recovery_file(
    isolated_personal_credentials,
):
    from lib.api_keys import bootstrap_personal_key, list_keys, revoke_key

    bootstrap_personal_key()
    key_id = list_keys(owner_user_id=1)[0]['id']

    assert revoke_key(key_id, owner_user_id=1)
    assert not isolated_personal_credentials.exists()


def test_unrelated_revoke_leaves_bootstrap_recovery_file(
    isolated_personal_credentials,
):
    from lib.api_keys import (
        bootstrap_personal_key, create_key, revoke_key,
    )

    bootstrap_personal_key()
    unrelated, _ = create_key(
        name='ci', scopes=['chat'], owner_user_id=1)
    revoke_key(unrelated['id'], owner_user_id=1)

    assert isolated_personal_credentials.exists()


def test_startup_purges_an_unknown_recovery_token(
    isolated_personal_credentials,
    caplog,
):
    from lib.api_keys import bootstrap_personal_key, create_key

    isolated_personal_credentials.write_text(
        'tofu_admin_' + 'a' * 32 + '\n', encoding='utf-8')
    create_key(
        name='other', scopes=['chat'], admin=True, owner_user_id=1)

    assert bootstrap_personal_key() is None
    assert not isolated_personal_credentials.exists()
    assert 'stale .first_run_token removed' in caplog.text

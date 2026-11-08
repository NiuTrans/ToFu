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


# A launcher that owns the process (local_kernel.py) supplies the install's
# secret (256 random bits) instead of letting the server mint one. Revoked
# credentials keep their unique hash, so every test admits its own secret.
@pytest.fixture
def supplied_secret():
    import secrets
    return 'tofu_admin_' + secrets.token_hex(32)


def test_a_supplied_secret_becomes_the_personal_admin_key_without_a_file(
    isolated_personal_credentials, supplied_secret,
):
    from lib.api_keys import install_personal_key, validate_token

    assert install_personal_key(supplied_secret) is True

    context = validate_token(supplied_secret)
    assert context is not None and context.has_scope('admin')
    assert context.owner_user_id == 1
    assert not isolated_personal_credentials.exists()


def test_later_boots_admit_only_the_same_supplied_secret(supplied_secret):
    from lib.api_keys import install_personal_key, list_keys

    assert install_personal_key(supplied_secret) is True
    assert install_personal_key(supplied_secret) is False
    with pytest.raises(PermissionError):
        install_personal_key('tofu_admin_' + 'f' * 64)
    assert len(list_keys(owner_user_id=1)) == 1


def test_a_revoked_install_secret_is_refused_at_the_next_boot(supplied_secret):
    from lib.api_keys import install_personal_key, list_keys, revoke_key

    install_personal_key(supplied_secret)
    revoke_key(list_keys(owner_user_id=1)[0]['id'], owner_user_id=1)

    with pytest.raises(PermissionError):
        install_personal_key(supplied_secret)
    assert list_keys(owner_user_id=1) == []


def test_an_owner_with_other_credentials_refuses_an_unknown_secret(supplied_secret):
    from lib.api_keys import create_key, install_personal_key, validate_token

    create_key(name='other', scopes=['chat'], admin=True, owner_user_id=1)

    with pytest.raises(PermissionError):
        install_personal_key(supplied_secret)
    assert validate_token(supplied_secret) is None


def test_a_non_admin_credential_is_not_an_install_secret():
    from lib.api_keys import create_key, install_personal_key

    _, live_token = create_key(name='chat-only', scopes=['chat'], owner_user_id=1)

    with pytest.raises(ValueError):
        install_personal_key(live_token)


@pytest.mark.parametrize('secret', [
    '',
    'tofu_admin_' + '0' * 32,
    'tofu_admin_' + '0' * 63,
    'tofu_admin_' + '0' * 65,
    'tofu_admin_' + 'A' * 64,
    ' tofu_admin_' + '0' * 64,
    'tofu_live_' + '0' * 64,
])
def test_malformed_secrets_are_refused_before_any_write(secret):
    from lib.api_keys import install_personal_key, list_keys, require_personal_key_secret

    with pytest.raises(ValueError):
        require_personal_key_secret(secret)
    with pytest.raises(ValueError):
        install_personal_key(secret)
    assert list_keys(owner_user_id=1) == []

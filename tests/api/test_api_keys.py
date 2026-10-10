"""Executable service contract for Sidecar-backed bearer credentials."""

from __future__ import annotations

import pytest

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit

OWNER = 1


def test_plaintext_is_returned_once_and_hash_never_leaves_storage():
    from lib.api_keys import create_key, list_keys

    row, plaintext = create_key(
        name='build-bot', scopes=['chat', 'tasks'], owner_user_id=OWNER)

    assert plaintext.startswith('tofu_live_')
    assert row['name'] == 'build-bot'
    assert row['scopes'] == ['chat', 'tasks']
    assert 'secret_hash' not in row
    assert all('secret_hash' not in key for key in list_keys(
        owner_user_id=OWNER))


def test_validation_carries_separate_owner_and_account_identity():
    from lib.api_keys import create_key, validate_token

    row, token = create_key(
        name='owner-key', scopes=['chat'], owner_user_id=OWNER)
    context = validate_token(token)

    assert context is not None
    assert context.key_id == row['id']
    assert context.owner_user_id == OWNER
    assert context.account_user_id == ''
    assert context.has_scope('chat')


def test_disabled_expired_and_revoked_credentials_fail_closed():
    from lib.api_keys import (
        create_key, identify_known_token, revoke_key, update_key,
        validate_token,
    )

    disabled, disabled_token = create_key(
        name='disabled', scopes=['chat'], owner_user_id=OWNER)
    assert update_key(
        disabled['id'], owner_user_id=OWNER, disabled=True)
    assert validate_token(disabled_token) is None

    expired, expired_token = create_key(
        name='expired', scopes=['chat'], owner_user_id=OWNER)
    assert update_key(expired['id'], owner_user_id=OWNER, expires_at=1.0)
    assert validate_token(expired_token) is None

    revoked, revoked_token = create_key(
        name='revoked', scopes=['chat'], owner_user_id=OWNER)
    assert revoke_key(revoked['id'], owner_user_id=OWNER)
    assert validate_token(revoked_token) is None
    tombstone = identify_known_token(revoked_token)
    assert tombstone is not None
    assert tombstone['owner_user_id'] == OWNER
    assert tombstone['revoked_at'] is not None
    assert 'secret_hash' not in tombstone
    assert revoke_key(revoked['id'], owner_user_id=OWNER) is False


def test_owner_boundary_hides_credentials_from_other_principals():
    from lib.api_keys import create_key, get_key_by_id, revoke_key

    row, _ = create_key(
        name='private', scopes=['chat'], owner_user_id=OWNER)

    assert get_key_by_id(row['id'], owner_user_id=99) is None
    assert revoke_key(row['id'], owner_user_id=99) is False
    assert get_key_by_id(row['id'], owner_user_id=OWNER) is not None


def test_scope_updates_cannot_change_token_privilege_tier():
    from lib.api_keys import create_key, get_key_by_id, update_key

    live, live_token = create_key(
        name='live', scopes=['chat'], owner_user_id=OWNER)
    assert live_token.startswith('tofu_live_')
    assert update_key(
        live['id'], owner_user_id=OWNER,
        scopes=['chat', 'tasks', 'admin'])
    updated_live = get_key_by_id(live['id'], owner_user_id=OWNER)
    assert updated_live['scopes'] == ['chat', 'tasks']

    admin, admin_token = create_key(
        name='admin', scopes=[], admin=True, owner_user_id=OWNER)
    assert admin_token.startswith('tofu_admin_')
    assert update_key(
        admin['id'], owner_user_id=OWNER, scopes=['chat'])
    updated_admin = get_key_by_id(admin['id'], owner_user_id=OWNER)
    assert {'admin', 'chat'} <= set(updated_admin['scopes'])


def test_unknown_token_shape_is_rejected_without_storage_lookup(monkeypatch):
    from lib.api_keys import validate_token

    monkeypatch.setattr(
        'lib.api_keys._validate.get_storage_client',
        lambda **_kwargs: pytest.fail('storage should not be called'),
    )
    assert validate_token('not-a-tofu-token') is None
    assert validate_token('') is None


def test_authcontext_admin_implicitly_grants_closed_scopes():
    from lib.api_keys import AuthContext

    context = AuthContext(
        key_id='x', name='a', owner_user_id=OWNER,
        scopes=frozenset({'admin'}),
    )
    assert context.has_scope('chat')
    assert context.has_scope('tasks')
    assert context.has_scope('agents:trading')

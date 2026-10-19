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


def test_new_and_historical_credentials_have_unrestricted_request_throughput(monkeypatch):
    from lib.api_keys import create_key, validate_token
    from lib.rate_limit.rate_limit_api import check_request
    import lib.rate_limit.rate_limit_store as store
    monkeypatch.setattr(store, 'get_token_bucket_store',
                        lambda: pytest.fail('uncapped credential initialized a quota backend'))
    for rpm in (None, 1, 60):
        options = {} if rpm is None else {'rate_limit_rpm': rpm}
        row, token = create_key(name='throughput', scopes=['chat'],
                                owner_user_id=OWNER, **options)
        assert row['rate_limit_rpm'] == (0 if rpm is None else rpm)
        context = validate_token(token)
        assert context is not None
        assert all(check_request(context, request_cost=4097).allowed
                   for _ in range(4097))


def test_http_key_creation_defaults_to_no_request_quota(monkeypatch):
    import asyncio
    import inspect
    from quart import Quart, g
    from lib.api_keys import local_admin_context
    import routes.api_v1.keys as keys
    observed = []
    def create(**fields):
        observed.append(fields)
        return {'id': 'synthetic-key', 'name': fields['name']}, 'synthetic-token'
    monkeypatch.setattr(keys, 'create_key', create)
    app = Quart(__name__)
    async def run():
        async with app.test_request_context('/api/v4/keys', method='POST',
                json={'name': 'throughput', 'scopes': ['chat']}):
            g.auth_ctx = local_admin_context()
            result = await app.ensure_async(inspect.unwrap(keys.create_key_route))()
            assert result is not None
    asyncio.run(run())
    assert observed[0]['rate_limit_rpm'] == 0
    metadata = keys.create_key_route._api_meta
    property_schema = metadata['request_body']['content']['application/json']['schema']['properties']['rate_limit_rpm']
    assert property_schema['default'] == 0
    assert property_schema['deprecated'] is True

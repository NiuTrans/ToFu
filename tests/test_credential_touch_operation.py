"""Sidecar contract for read validation and conditional audit touches."""

from __future__ import annotations

import pytest


pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit


def test_validate_is_read_only_and_touch_is_conditional(credential_storage):
    client = credential_storage.client(write=True)
    created = client.command('credential.create', {
        'credential_id': 'touch-budget-key',
        'owner_user_id': 9,
        'account_user_id': '',
        'tenant_id': 'tenant-a',
        'name': 'Touch budget',
        'prefix': 'tofu_live_touch',
        'secret_hash': 'c' * 64,
        'scopes': ['chat'],
        'rate_limit_rpm': 60,
        'rate_limit_tpd': 0,
        'created_at': 100.0,
        'expires_at': None,
        'metadata': {},
    }, 'create-touch-budget-key')
    assert created['last_used_at'] is None

    validated = client.query('credential.validate', {
        'secret_hash': 'c' * 64,
        'now': 200.0,
    })
    assert validated['id'] == 'touch-budget-key'
    assert validated['last_used_at'] is None

    first = client.command('credential.touch', {
        'credential_id': 'touch-budget-key',
        'owner_user_id': 9,
        'tenant_id': 'tenant-a',
        'used_at': 200.0,
        'touch_if_before': 140.0,
    }, None, priority='maintenance')
    second = client.command('credential.touch', {
        'credential_id': 'touch-budget-key',
        'owner_user_id': 9,
        'tenant_id': 'tenant-a',
        'used_at': 210.0,
        'touch_if_before': 150.0,
    }, None, priority='maintenance')

    assert first == {'touched': True}
    assert second == {'touched': False}
    assert client.query('credential.validate', {
        'secret_hash': 'c' * 64,
        'now': 211.0,
    })['last_used_at'] == 200.0


def test_touch_cannot_cross_owner_boundary_or_revocation(credential_storage):
    client = credential_storage.client(write=True)
    client.command('credential.create', {
        'credential_id': 'touch-boundary-key',
        'owner_user_id': 11,
        'account_user_id': '',
        'tenant_id': '',
        'name': 'Touch boundary',
        'prefix': 'tofu_live_bound',
        'secret_hash': 'd' * 64,
        'scopes': ['chat'],
        'rate_limit_rpm': 60,
        'rate_limit_tpd': 0,
        'created_at': 100.0,
        'expires_at': None,
        'metadata': {},
    }, 'create-touch-boundary-key')
    payload = {
        'credential_id': 'touch-boundary-key',
        'owner_user_id': 12,
        'tenant_id': '',
        'used_at': 200.0,
        'touch_if_before': 140.0,
    }
    assert client.command(
        'credential.touch', payload, None, priority='maintenance',
    ) == {'touched': False}

    client.command('credential.revoke', {
        'credential_id': 'touch-boundary-key',
        'owner_user_id': 11,
        'tenant_id': '',
        'revoked_at': 201.0,
    }, 'revoke-touch-boundary-key')
    assert client.command('credential.touch', {
        **payload,
        'owner_user_id': 11,
        'used_at': 202.0,
    }, None, priority='maintenance') == {'touched': False}

"""Executable guard for contracts/identity_v1.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytest_plugins = ('tests._credential_sidecar',)
pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = yaml.safe_load(
    (ROOT / 'contracts/identity_v1.yaml').read_text(encoding='utf-8'))


def test_declared_identity_operations_are_the_registered_authority():
    from lib.storage_sidecar.operation_domains.identity import OPERATIONS

    declared = {
        operation
        for authority in CONTRACT['authorities'].values()
        for operation in authority['operations']
    }
    assert declared == set(OPERATIONS)
    assert OPERATIONS['tenant.user.create'].receipt_required
    assert OPERATIONS['credential.create'].receipt_required
    assert not OPERATIONS['credential.authenticate'].receipt_required


def test_identifier_shapes_match_runtime_contexts():
    from lib.api_keys import AuthContext
    from lib.identity import PERSONAL_USER_ID, principal_from_auth_context

    assert PERSONAL_USER_ID == CONTRACT['identifiers']['ownerUserId'][
        'personalReservedValue']
    context = AuthContext(
        key_id='credential-7',
        owner_user_id=7,
        account_user_id='usr_opaque',
        tenant_id='tenant-a',
        scopes=frozenset({'chat'}),
    )
    principal = principal_from_auth_context(
        context, allow_personal_owner=False)
    assert principal.owner_user_id == 7
    assert principal.subject_id == 'credential-7'
    assert principal.tenant_id == 'tenant-a'
    assert principal.subject_id != context.account_user_id


def test_device_bridge_has_no_global_or_credential_free_authority(monkeypatch):
    from lib.bridge_auth import process_agent_token, resolve_bridge_credential

    monkeypatch.setenv('TOFU_BRIDGE_SECRET', 'obsolete-global-secret')
    assert resolve_bridge_credential('obsolete-global-secret') is None
    assert resolve_bridge_credential('') is None
    assert resolve_bridge_credential(process_agent_token()) is None
    process_context = resolve_bridge_credential(
        process_agent_token(), allow_process_agent=True)
    assert process_context is not None
    assert process_context.owner_user_id == 1
    assert process_context.scopes == frozenset({'agents:bridge'})


def test_schema_contains_separate_account_owner_and_credential_authorities():
    schema = (ROOT / 'lib/storage_sidecar/schema.py').read_text(encoding='utf-8')
    assert 'tenant_users (' in schema
    assert 'owner_user_id BIGINT NOT NULL UNIQUE' in schema
    assert 'auth_credentials (' in schema
    assert 'account_user_id TEXT NOT NULL' in schema
    assert 'secret_hash TEXT NOT NULL UNIQUE' in schema

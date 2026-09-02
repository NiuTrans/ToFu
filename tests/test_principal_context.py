"""Executable identity contract for request, task, and system boundaries."""

import pytest

from lib.identity import PrincipalContext, principal_from_auth_context


pytestmark = pytest.mark.unit


def test_user_principal_round_trip_and_scope_enforcement():
    principal = PrincipalContext.user(
        subject_id='key-42', owner_user_id='42', tenant_id='future-tenant',
        scopes={'chat', 'tasks'})

    restored = PrincipalContext.from_payload(principal.to_payload())

    assert restored == principal
    assert restored.require_owner(context='task') == 42
    restored.require_scope('tasks')
    with pytest.raises(PermissionError, match='lacks scope'):
        restored.require_scope('admin')


def test_system_principal_is_restricted_and_has_no_implicit_owner():
    principal = PrincipalContext.system(
        subject_id='maintenance.backup', scopes={'maintenance:backup'})

    principal.require_scope('maintenance:backup')
    with pytest.raises(PermissionError, match='owning user'):
        principal.require_owner(context='user task')
    with pytest.raises(PermissionError, match='lacks scope'):
        principal.require_scope('maintenance:retention')


def test_task_runtime_accepts_principal_and_rejects_owner_mismatch():
    from lib.agent_core.task_runtime import TaskRuntime

    runtime = TaskRuntime('principal-task')
    principal = PrincipalContext.user(
        subject_id='key-task', owner_user_id=7, scopes={'tasks'})
    task = runtime.create(principal=principal)
    assert task['_userId'] == 7
    assert task['_principalContext'] == principal.to_payload()

    with pytest.raises(ValueError, match='mismatch'):
        runtime.create(principal=principal, user_id=8)
    with pytest.raises(PermissionError, match='owning user'):
        runtime.create(principal=PrincipalContext.system(
            subject_id='maintenance', scopes={'maintenance:backup'}))


def test_auth_adapter_maps_personal_only_when_composition_allows_it():
    class Context:
        key_id = 'local'
        owner_user_id = None
        account_user_id = ''
        tenant_id = None
        scopes = frozenset({'admin'})

    principal = principal_from_auth_context(
        Context(), allow_personal_owner=True)
    assert principal.owner_user_id == 1
    assert principal.subject_id == 'local'

    with pytest.raises(PermissionError, match='owner_user_id'):
        principal_from_auth_context(Context(), allow_personal_owner=False)


def test_auth_adapter_never_coerces_an_opaque_account_id_into_owner():
    class Context:
        key_id = 'tenant-key'
        owner_user_id = 17
        account_user_id = 'usr_public_opaque'
        tenant_id = None
        scopes = frozenset({'chat'})

    principal = principal_from_auth_context(
        Context(), allow_personal_owner=False)

    assert principal.owner_user_id == 17
    assert principal.subject_id == 'tenant-key'


@pytest.mark.parametrize('owner', [None, '', 0, -1, True, 'alice'])
def test_user_principal_rejects_invalid_owner(owner):
    with pytest.raises(ValueError):
        PrincipalContext.user(subject_id='invalid-owner', owner_user_id=owner)

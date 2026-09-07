"""Account, internal-owner, and bearer-credential operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'tenant.user.create': ops.OperationSpec('command', True, ops._tenant_user_create),
    'tenant.user.get': ops.OperationSpec('query', False, ops._tenant_user_get),
    'tenant.user.list': ops.OperationSpec('query', False, ops._tenant_user_list),
    'tenant.user.set_status': ops.OperationSpec(
        'command', True, ops._tenant_user_set_status),
    'tenant.user.set_role': ops.OperationSpec(
        'command', True, ops._tenant_user_set_role),
    'tenant.user.authentication': ops.OperationSpec(
        'query', False, ops._tenant_user_authentication),
    'tenant.user.record_login': ops.OperationSpec(
        'command', True, ops._tenant_user_record_login),
    'credential.create': ops.OperationSpec(
        'command', True, ops._credential_create),
    'credential.create_if_owner_empty': ops.OperationSpec(
        'command', False, ops._credential_create_if_owner_empty),
    'credential.list': ops.OperationSpec(
        'query', False, ops._credential_list),
    'credential.get': ops.OperationSpec(
        'query', False, ops._credential_get),
    'credential.authenticate': ops.OperationSpec(
        'command', False, ops._credential_authenticate),
    'credential.validate': ops.OperationSpec(
        'query', False, ops._credential_validate),
    'credential.touch': ops.OperationSpec(
        'command', False, ops._credential_touch),
    'credential.identify': ops.OperationSpec(
        'query', False, ops._credential_identify),
    'credential.update': ops.OperationSpec(
        'command', True, ops._credential_update),
    'credential.revoke': ops.OperationSpec(
        'command', True, ops._credential_revoke),
    'credential.exists': ops.OperationSpec(
        'query', False, ops._credential_exists),
}

__all__ = ['OPERATIONS']

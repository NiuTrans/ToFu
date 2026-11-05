"""Application import port for the standard-library process-role authority.

Definitions live in bootstrap_pkg.process_roles so launcher validation never
loads application configuration merely to validate a lifecycle owner.
"""
from bootstrap_pkg.process_roles import (
    CAPABILITY_EVENT_MAINTENANCE,
    CAPABILITY_FRONTEND,
    CAPABILITY_NETWORK_CONFIGURATION,
    CAPABILITY_REQUEST_SERVICES,
    CAPABILITY_SCHEDULED_JOBS,
    CAPABILITY_TASK_RECOVERY,
    CAPABILITY_TASK_WORKERS,
    VALID_PROCESS_ROLES,
    PERSONAL_PROCESS_ROLES,
    capabilities_for_role,
    normalize_process_role,
    process_role_has,
    process_role_has_any,
)

__all__ = [
    'CAPABILITY_EVENT_MAINTENANCE',
    'CAPABILITY_FRONTEND',
    'CAPABILITY_NETWORK_CONFIGURATION',
    'CAPABILITY_REQUEST_SERVICES',
    'CAPABILITY_SCHEDULED_JOBS',
    'CAPABILITY_TASK_RECOVERY',
    'CAPABILITY_TASK_WORKERS',
    'VALID_PROCESS_ROLES',
    'PERSONAL_PROCESS_ROLES',
    'capabilities_for_role',
    'normalize_process_role',
    'process_role_has',
    'process_role_has_any',
]

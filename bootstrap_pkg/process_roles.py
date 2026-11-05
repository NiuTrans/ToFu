"""Standard-library deployment roles and lifecycle capabilities.

The deployment contract is parsed in :mod:`runtime_guards`; this module is
the single runtime mapping from that validated role to owned work.  Callers
ask for a named capability instead of copying role comparisons, so adding a
new owner cannot silently start it in every Kubernetes replica.
"""

from __future__ import annotations

# pyright: strict

from collections.abc import Iterable


CAPABILITY_FRONTEND = 'frontend'
CAPABILITY_REQUEST_SERVICES = 'request_services'
CAPABILITY_NETWORK_CONFIGURATION = 'network_configuration'
CAPABILITY_TASK_RECOVERY = 'task_recovery'
CAPABILITY_TASK_WORKERS = 'task_workers'
CAPABILITY_SCHEDULED_JOBS = 'scheduled_jobs'
CAPABILITY_EVENT_MAINTENANCE = 'event_maintenance'

_ALL_CAPABILITIES = frozenset({
    CAPABILITY_FRONTEND,
    CAPABILITY_REQUEST_SERVICES,
    CAPABILITY_NETWORK_CONFIGURATION,
    CAPABILITY_TASK_RECOVERY,
    CAPABILITY_TASK_WORKERS,
    CAPABILITY_SCHEDULED_JOBS,
    CAPABILITY_EVENT_MAINTENANCE,
})

_ROLE_CAPABILITIES = {
    # All-in-one embedding and explicit single-replica composition share
    # the same capability vocabulary as managed split processes.
    'all': _ALL_CAPABILITIES,
    'api': frozenset({
        CAPABILITY_FRONTEND,
        CAPABILITY_REQUEST_SERVICES,
        CAPABILITY_NETWORK_CONFIGURATION,
    }),
    'worker': frozenset({
        CAPABILITY_NETWORK_CONFIGURATION,
        CAPABILITY_TASK_RECOVERY,
        CAPABILITY_TASK_WORKERS,
    }),
    'scheduler': frozenset({
        CAPABILITY_SCHEDULED_JOBS,
        CAPABILITY_EVENT_MAINTENANCE,
    }),
}


_ROLE_CAPABILITIES['execution'] = (
    _ROLE_CAPABILITIES['worker'] | _ROLE_CAPABILITIES['scheduler'])
VALID_PROCESS_ROLES = frozenset(_ROLE_CAPABILITIES)
PERSONAL_PROCESS_ROLES = frozenset({'all', 'api', 'execution'})


def normalize_process_role(role: str) -> str:
    """Return a validated canonical process role."""
    normalized = str(role or '').strip().lower()
    if normalized not in VALID_PROCESS_ROLES:
        raise ValueError(
            'process role must be one of: ' + ', '.join(sorted(VALID_PROCESS_ROLES)))
    return normalized


def capabilities_for_role(role: str) -> frozenset[str]:
    """Return the immutable capability set owned by ``role``."""
    return _ROLE_CAPABILITIES[normalize_process_role(role)]


def process_role_has(role: str, capability: str) -> bool:
    """Return whether ``role`` owns one declared lifecycle capability."""
    if capability not in _ALL_CAPABILITIES:
        raise ValueError(f'unknown process-role capability: {capability}')
    return capability in capabilities_for_role(role)


def process_role_has_any(role: str, capabilities: Iterable[str]) -> bool:
    """Return whether ``role`` owns at least one requested capability."""
    owned = capabilities_for_role(role)
    requested = tuple(capabilities)
    unknown = set(requested).difference(_ALL_CAPABILITIES)
    if unknown:
        raise ValueError(
            'unknown process-role capabilities: ' + ', '.join(sorted(unknown)))
    return bool(owned.intersection(requested))


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

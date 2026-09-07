"""Single versioned loader for the Sidecar's semantic operation catalog."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from lib.storage_sidecar.operation_domains import REGISTRY_VERSION

if TYPE_CHECKING:
    from lib.storage_sidecar.operations import OperationSpec


_DOMAIN_MODULES = (
    'core', 'identity', 'providers', 'model_routing', 'artifacts', 'billing', 'integration', 'workflows', 'research',
    'knowledge',
    'queue', 'worker_jobs', 'conversations', 'archives', 'timers', 'scheduler', 'turns',
    'observability', 'plugins', 'desktop', 'browser', 'project_brain',
)


def build_registry() -> dict[str, 'OperationSpec']:
    catalog: dict[str, OperationSpec] = {}
    for domain in _DOMAIN_MODULES:
        module = import_module(
            f'lib.storage_sidecar.operation_domains.{domain}')
        for name, spec in module.OPERATIONS.items():
            if name in catalog:
                raise RuntimeError(f'duplicate storage operation: {name}')
            catalog[name] = spec
    return catalog


__all__ = ['REGISTRY_VERSION', 'build_registry']

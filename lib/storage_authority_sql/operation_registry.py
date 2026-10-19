"""Single versioned loader for the Sidecar's semantic operation catalog."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

from lib.storage_authority_sql.operation_domains import REGISTRY_VERSION

if TYPE_CHECKING:
    from lib.storage_authority_sql.operations import OperationSpec


_DOMAIN_MODULES = (
    'core', 'automations', 'identity', 'providers', 'model_routing', 'artifacts', 'billing', 'integration', 'workflows', 'research',
    'knowledge', 'paper_artifacts', 'paper_documents',
    'queue', 'worker_jobs', 'conversations', 'archives', 'timers', 'scheduler', 'turns',
    'observability', 'plugins', 'desktop', 'browser', 'project_brain', 'project_dispatch',
)


def build_registry() -> dict[str, 'OperationSpec']:
    catalog: dict[str, OperationSpec] = {}
    for domain in _DOMAIN_MODULES:
        module = import_module(
            f'lib.storage_authority_sql.operation_domains.{domain}')
        for name, spec in module.OPERATIONS.items():
            if name in catalog:
                raise RuntimeError(f'duplicate storage operation: {name}')
            catalog[name] = spec
    return catalog


__all__ = ['REGISTRY_VERSION', 'build_registry']

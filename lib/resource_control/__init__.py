"""Process resource-control port. Importing it creates no controller or worker."""
from __future__ import annotations

from .contract import ResourceLease, ResourceOwner, ResourcePlan, ResourcePort, ResourceSample
from .controller import ResourceConflict, ResourceController

_current: ResourceController | None = None


def current_controller() -> ResourceController | None:
    return _current


def install_controller(controller: ResourceController) -> None:
    global _current
    _current = controller


def uninstall_controller(controller: ResourceController) -> None:
    global _current
    if _current is controller:
        _current = None
    controller.close()


def resource_snapshot() -> dict:
    controller = _current
    return controller.snapshot() if controller else {'active': False, 'resources': {}}


__all__ = ['ResourceController', 'ResourceConflict', 'ResourceLease', 'ResourceOwner',
           'ResourcePlan', 'ResourcePort', 'ResourceSample', 'current_controller',
           'install_controller', 'uninstall_controller', 'resource_snapshot']

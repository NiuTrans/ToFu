"""Task-orchestration namespace.

Use :mod:`lib.tasks_pkg.orchestrator.api` to start a task. Turn drivers,
round policy and finalization helpers live in their concrete modules. Runtime
dependency injection is owned by ``_ports``; the package root has no mutable
facade or compatibility exports.
"""

__all__ = ()

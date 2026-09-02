"""Context-compaction domain namespace.

Application services are declared in :mod:`lib.tasks_pkg.compaction.api`.
Algorithms, policies, persistence, and tunables remain in their concrete
owner modules.  The package root intentionally performs no registration and
exports no facade symbols.
"""

__all__: tuple[str, ...] = ()

"""Durable swarm-session status vocabulary shared with the Storage Sidecar.

This module is dependency-free on purpose. The swarm runtime and the Sidecar
operation catalog both import it, while neither becomes an owner of the
other's implementation details.
"""

from __future__ import annotations


SWARM_SESSION_STATUS_RUNNING = "running"
SWARM_SESSION_STATUS_TERMINATED = "terminated"
SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS = "quarantined:ownerless"

# Only ordinary lifecycle code may write these states. Quarantine has a
# dedicated semantic operation so a caller cannot bypass its owner check.
SWARM_SESSION_WRITABLE_STATUSES = frozenset({
    SWARM_SESSION_STATUS_RUNNING,
    SWARM_SESSION_STATUS_TERMINATED,
})

SWARM_SESSION_TERMINAL_STATUSES = frozenset({
    SWARM_SESSION_STATUS_TERMINATED,
    SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS,
})


__all__ = [
    "SWARM_SESSION_STATUS_RUNNING",
    "SWARM_SESSION_STATUS_TERMINATED",
    "SWARM_SESSION_STATUS_QUARANTINED_OWNERLESS",
    "SWARM_SESSION_WRITABLE_STATUSES",
    "SWARM_SESSION_TERMINAL_STATUSES",
]

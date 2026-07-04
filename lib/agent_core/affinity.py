"""lib/agent_core/affinity.py — Consistent-hash task→replica affinity (Epic C §4.1).

Sticky sessions pin a task's stream/poll/abort requests to the replica that
owns its live in-memory object. The AUTHORITATIVE routing lives at the load
balancer (consistent-hash on the ``taskId`` path segment — see the Epic C
design). This module is the app-side MIRROR of that hash so the server can:

  * advertise the owning replica for a task (diagnostics / a client hint), and
  * let a replica cheaply answer "is this task mine?" without a cross-replica
    probe (the ratified §6.4 "no liveness probe" rule).

Pure + dependency-free: a stable hash (blake2b) of the taskId modulo the ring.
Under a single replica (the default) ``owns_task`` is always True, so behaviour
is byte-identical to today.
"""

from __future__ import annotations

import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)


def _replica_id() -> str:
    return os.environ.get('TOFU_REPLICA_ID') or ('pid-%d' % os.getpid())


def _ring() -> list[str]:
    """The ordered replica ring, from ``TOFU_REPLICA_RING`` (comma-separated).

    Empty/unset → single-replica ring containing just this replica, so every
    task is owned locally (byte-identical single-box behaviour)."""
    raw = (os.environ.get('TOFU_REPLICA_RING') or '').strip()
    if not raw:
        return [_replica_id()]
    ring = [r.strip() for r in raw.split(',') if r.strip()]
    return ring or [_replica_id()]


def owner_replica(task_id: str, ring: list[str] | None = None) -> str:
    """Return the replica id that OWNS ``task_id`` under consistent hashing.

    Deterministic across replicas given the same ring, so every replica agrees
    on the owner without coordination — the property the LB affinity relies on.
    """
    r = ring if ring is not None else _ring()
    if len(r) <= 1:
        return r[0] if r else _replica_id()
    h = hashlib.blake2b(task_id.encode('utf-8'), digest_size=8).digest()
    idx = int.from_bytes(h, 'big') % len(r)
    return r[idx]


def owns_task(task_id: str, ring: list[str] | None = None) -> bool:
    """True iff THIS replica is the consistent-hash owner of ``task_id``.

    Always True on a single-replica ring (default) → no behaviour change."""
    return owner_replica(task_id, ring) == _replica_id()


__all__ = ['owner_replica', 'owns_task']

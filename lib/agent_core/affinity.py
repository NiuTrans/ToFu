"""lib/agent_core/affinity.py — Replica-affinity diagnostics (Epic C §4.1).

The authoritative browser routing lives at the load balancer: it hashes the
``X-Tofu-Affinity-Key`` that the browser derives *before* task creation (stable
per conversation, random only for conversation-less tasks) and reuses for
SSE/poll/abort. That avoids requiring Python to mirror an implementation-
specific nginx/Envoy hash.

This dependency-free helper remains useful for diagnostics and non-browser
deployments that assign task ids ahead of dispatch. It uses Highest-Random-
Weight (rendezvous) hashing: unlike ``hash % replica_count``, adding/removing a
replica only moves keys to/from that replica instead of remapping most keys.
"""

from __future__ import annotations

import hashlib
import os

from lib.log import get_logger

logger = get_logger(__name__)


def replica_id() -> str:
    return os.environ.get('TOFU_REPLICA_ID') or ('pid-%d' % os.getpid())


def _ring() -> list[str]:
    """The ordered replica ring, from ``TOFU_REPLICA_RING`` (comma-separated).

    Empty/unset → single-replica ring containing just this replica, so every
    task is owned locally (byte-identical single-box behaviour)."""
    raw = (os.environ.get('TOFU_REPLICA_RING') or '').strip()
    if not raw:
        return [replica_id()]
    ring = [r.strip() for r in raw.split(',') if r.strip()]
    return ring or [replica_id()]


def owner_replica(task_id: str, ring: list[str] | None = None) -> str:
    """Return the replica id that OWNS ``task_id`` under consistent hashing.

    Deterministic across replicas given the same ring, so every replica agrees
    on the owner without coordination — the property the LB affinity relies on.
    """
    r = list(dict.fromkeys(ring if ring is not None else _ring()))
    if len(r) <= 1:
        return r[0] if r else replica_id()

    def _score(replica: str) -> int:
        digest = hashlib.blake2b(
            (str(task_id) + '\0' + replica).encode('utf-8'),
            digest_size=16).digest()
        return int.from_bytes(digest, 'big')

    return max(r, key=lambda replica: (_score(replica), replica))


def owns_task(task_id: str, ring: list[str] | None = None) -> bool:
    """True iff THIS replica is the consistent-hash owner of ``task_id``.

    Always True on a single-replica ring (default) → no behaviour change."""
    return owner_replica(task_id, ring) == replica_id()


__all__ = ['replica_id', 'owner_replica', 'owns_task']

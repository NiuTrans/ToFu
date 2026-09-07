"""Bound overlapping browser snapshot reads by owner, page, and conversation.

Responsibility
--------------
Contain a broken or stale browser page that recursively requests the same
multi-megabyte conversation snapshot before its first request completes. The
gate is process-local, keeps only fixed-size identity digests, and fails open
when its bounded registry is full so admission can never make snapshots
globally unavailable.

Entry points
------------
``ConversationSnapshotAdmission.enter`` returns an explicit decision and
``release`` closes an admitted request in the route's ``finally`` boundary.

Dependencies
------------
The HTTP adapter owns browser-page identity validation. This module owns only
concurrency state; storage and authorization remain outside this boundary.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass

from lib.log import get_logger
from runtime_guards import resolve_resource_budget


_MAX_ACTIVE_SNAPSHOTS = 256
logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SnapshotAdmissionLease:
    """Opaque ownership token for one tracked snapshot request."""

    identity_digest: bytes
    generation: int


@dataclass(frozen=True, slots=True)
class SnapshotAdmissionDecision:
    """One route-facing decision; allowed capacity bypasses have no lease."""

    allowed: bool
    reason: str
    lease: SnapshotAdmissionLease | None = None


class ConversationSnapshotAdmission:
    """Reject only overlapping snapshots from the same authenticated page."""

    def __init__(self, *, max_active: int | None = None) -> None:
        resolved = (
            resolve_resource_budget(
                "TOFU_STORAGE_RPC_CAPACITY", maximum=_MAX_ACTIVE_SNAPSHOTS,
            )
            if max_active is None else int(max_active)
        )
        self.max_active = max(1, min(_MAX_ACTIVE_SNAPSHOTS, resolved))
        self._lock = threading.Lock()
        self._active: dict[bytes, int] = {}
        self._generation = 0
        self._rejected = 0
        self._capacity_bypassed = 0
        self._peak_active = 0

    @staticmethod
    def _identity_digest(
        *,
        user_id: int,
        conversation_id: str,
        page_id: str,
        representation: str,
    ) -> bytes:
        material = "\0".join((
            str(int(user_id)),
            str(conversation_id),
            str(page_id),
            str(representation),
        )).encode("utf-8", errors="replace")
        return hashlib.sha256(material).digest()

    def enter(
        self,
        *,
        user_id: int,
        conversation_id: str,
        page_id: str,
        representation: str,
    ) -> SnapshotAdmissionDecision:
        """Admit one request or reject an exact active-page overlap."""
        identity = self._identity_digest(
            user_id=user_id,
            conversation_id=conversation_id,
            page_id=page_id,
            representation=representation,
        )
        should_log = False
        rejection_count = 0
        with self._lock:
            if identity in self._active:
                self._rejected += 1
                rejection_count = self._rejected
                should_log = (
                    rejection_count <= 2
                    or (rejection_count & (rejection_count - 1)) == 0
                )
                decision = SnapshotAdmissionDecision(
                    allowed=False,
                    reason="snapshot_in_flight",
                )
            elif len(self._active) >= self.max_active:
                # The registry itself is a memory budget, never an availability
                # ceiling. Unknown identities continue through the normal read.
                self._capacity_bypassed += 1
                decision = SnapshotAdmissionDecision(
                    allowed=True,
                    reason="capacity_bypass",
                )
            else:
                self._generation += 1
                lease = SnapshotAdmissionLease(identity, self._generation)
                self._active[identity] = lease.generation
                self._peak_active = max(self._peak_active, len(self._active))
                decision = SnapshotAdmissionDecision(
                    allowed=True,
                    reason="admitted",
                    lease=lease,
                )
        if should_log:
            logger.warning(
                "[ConversationSnapshotAdmission] rejected overlapping "
                "snapshot count=%d active=%d",
                rejection_count,
                self.active_count(),
            )
        return decision

    def release(self, lease: SnapshotAdmissionLease | None) -> None:
        """Release exactly the generation acquired by ``enter``; idempotent."""
        if lease is None:
            return
        with self._lock:
            if self._active.get(lease.identity_digest) == lease.generation:
                self._active.pop(lease.identity_digest, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._active)

    def snapshot(self) -> dict[str, int]:
        """Return bounded counters without owner, page, or conversation IDs."""
        with self._lock:
            return {
                "capacity": self.max_active,
                "active": len(self._active),
                "peakActive": self._peak_active,
                "rejected": self._rejected,
                "capacityBypassed": self._capacity_bypassed,
            }


snapshot_admission = ConversationSnapshotAdmission()


__all__ = [
    "ConversationSnapshotAdmission",
    "SnapshotAdmissionDecision",
    "SnapshotAdmissionLease",
    "snapshot_admission",
]

"""Bounded, owner-aware registry for blocking human decisions.

The three task-facing human gates differ in wait policy, but their mutable
state machine is identical: register one waiter, accept exactly one answer,
and let the waiter atomically consume or abandon it.  Centralizing that state
machine prevents race fixes and authorization rules from drifting between
stdin, guidance, and write approval.

Entries remain process-local because an Event cannot cross replicas.  The
existing enterprise migration seam is documented by the callers; this owner
still bounds personal-mode memory and rejects cross-owner resolution now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Generic, TypeVar

from lib.human_gate_contract import require_human_gate_request_id
from lib.identity import require_user_id
from runtime_guards import resolve_resource_budget


ResponseT = TypeVar('ResponseT')

GATE_STDIN = 'stdin'
GATE_GUIDANCE = 'guidance'
GATE_WRITE_APPROVAL = 'write_approval'
_VALID_GATE_KINDS = frozenset({
    GATE_STDIN,
    GATE_GUIDANCE,
    GATE_WRITE_APPROVAL,
})


def _default_capacity() -> int:
    """Size waiters from the launch-time active-task budget.

    A task normally owns at most one blocking human gate.  Two slots per
    admitted task leave room for nested orchestration transitions without
    turning abandoned request IDs into an unbounded process-lifetime map.
    """
    active_tasks = resolve_resource_budget(
        'TOFU_MAX_INFLIGHT_TASKS', minimum=1, maximum=256)
    return max(8, min(512, active_tasks * 2))


@dataclass
class HumanGateEntry(Generic[ResponseT]):
    owner_user_id: int
    event: threading.Event = field(default_factory=threading.Event)
    resolved: bool = False
    response: ResponseT | None = None


@dataclass(frozen=True)
class HumanGateResolution(Generic[ResponseT]):
    found: bool
    resolved: bool
    response: ResponseT | None = None


class OwnedHumanGateRegistry:
    """Thread-safe first-resolution-wins registry with a hard capacity."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or int(capacity) != capacity \
                or int(capacity) <= 0:
            raise ValueError('human gate capacity must be a positive integer')
        self._capacity = int(capacity)
        self._entries: dict[tuple[str, str], HumanGateEntry[object]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(kind: str, request_id: object) -> tuple[str, str]:
        if kind not in _VALID_GATE_KINDS:
            raise ValueError(f'unknown human gate kind: {kind!r}')
        return kind, require_human_gate_request_id(
            request_id, field=f'{kind} request id')

    def register(
        self,
        kind: str,
        request_id: object,
        *,
        owner_user_id: object,
    ) -> HumanGateEntry[object] | None:
        key = self._key(kind, request_id)
        owner = require_user_id(
            owner_user_id, context=f'{kind} request owner')
        entry: HumanGateEntry[object] = HumanGateEntry(owner_user_id=owner)
        with self._lock:
            if key in self._entries or len(self._entries) >= self._capacity:
                return None
            self._entries[key] = entry
        return entry

    def resolve(
        self,
        kind: str,
        request_id: object,
        *,
        owner_user_id: object,
        response: object,
    ) -> bool:
        key = self._key(kind, request_id)
        owner = require_user_id(
            owner_user_id, context=f'{kind} response owner')
        with self._lock:
            entry = self._entries.get(key)
            # An owner mismatch is intentionally indistinguishable from an
            # unknown ID so this registry cannot be used as an existence
            # oracle across tenants.
            if (entry is None or entry.owner_user_id != owner
                    or entry.resolved):
                return False
            entry.resolved = True
            entry.response = response
            entry.event.set()
            return True

    def take(
        self,
        kind: str,
        request_id: object,
        entry: HumanGateEntry[object],
    ) -> HumanGateResolution[object]:
        """Atomically remove and return the exact registered entry."""
        key = self._key(kind, request_id)
        with self._lock:
            current = self._entries.get(key)
            if current is not entry:
                return HumanGateResolution(found=False, resolved=False)
            self._entries.pop(key, None)
            return HumanGateResolution(
                found=True,
                resolved=current.resolved,
                response=current.response,
            )

    def discard_unresolved(
        self,
        kind: str,
        request_id: object,
        entry: HumanGateEntry[object],
    ) -> bool:
        """Remove ``entry`` only if a resolver has not already won."""
        key = self._key(kind, request_id)
        with self._lock:
            current = self._entries.get(key)
            if current is not entry or current.resolved:
                return False
            self._entries.pop(key, None)
            return True

    def is_pending(
        self,
        kind: str,
        request_id: object,
        *,
        owner_user_id: object,
    ) -> bool:
        key = self._key(kind, request_id)
        owner = require_user_id(
            owner_user_id, context=f'{kind} pending-query owner')
        with self._lock:
            entry = self._entries.get(key)
            return bool(entry is not None and entry.owner_user_id == owner)

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


human_gate_registry = OwnedHumanGateRegistry(_default_capacity())


__all__ = [
    'GATE_GUIDANCE',
    'GATE_STDIN',
    'GATE_WRITE_APPROVAL',
    'HumanGateEntry',
    'HumanGateResolution',
    'OwnedHumanGateRegistry',
    'human_gate_registry',
]

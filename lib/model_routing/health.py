"""Bounded runtime health with failure scope matching model-routing entities.

``owner_health_registry`` returns the process-local table of one explicit
routing owner and tenant. Tables are reconstructible evidence, never
configuration: they are LRU-bounded by owner count and never shared across
owners or tenants.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import threading
import time
from typing import Callable

# A subscription login source (for example a missing Codex home) cannot issue
# a token before provider ingress. Every route that resolves through the same
# credential fails identically until the source is repaired.
CREDENTIAL_SOURCE_UNAVAILABLE = "credential_source_unavailable"
MAX_OWNER_HEALTH_REGISTRIES = 1024


@dataclass(frozen=True, slots=True)
class HealthTarget:
    scope: str
    entity_id: str
    related_id: str = ""


@dataclass(slots=True)
class _HealthEntry:
    failures: int
    unavailable_until: float
    expires_at: float
    reason: str


def classify_failure(
    *, status_code: int | None = None, kind: str = "",
) -> str:
    """Map one typed failure to its narrowest truthful health scope."""
    normalized = str(kind or "").lower()
    if normalized in {
        "route_missing", "model_not_found", "deployment_not_found",
        "upstream_model_not_found",
    } or status_code == 404:
        return "deployment"
    if status_code in {401, 402} or normalized in {
        "credential_rejected", "payment_required", "invalid_api_key",
        CREDENTIAL_SOURCE_UNAVAILABLE,
    }:
        return "credential"
    if status_code == 403 or normalized in {
        "credential_deployment_forbidden", "model_entitlement_denied",
    }:
        return "credential_deployment"
    if normalized in {
        "network", "connect_timeout", "connection_error", "tls_error",
        "proxy_error", "stream_idle_timeout",
    }:
        return "connection"
    return "deployment"


class RouteHealthRegistry:
    """Process-local bounded health table; configuration remains separate."""

    def __init__(
        self,
        *,
        max_entries: int = 4096,
        ttl_seconds: float = 3600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_entries = max(64, min(int(max_entries), 16384))
        self.ttl_seconds = max(60.0, min(float(ttl_seconds), 24 * 3600.0))
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: OrderedDict[HealthTarget, _HealthEntry] = OrderedDict()

    @staticmethod
    def target_for_failure(
        candidate,
        *,
        status_code: int | None = None,
        kind: str = "",
    ) -> HealthTarget:
        scope = classify_failure(status_code=status_code, kind=kind)
        if scope == "connection":
            return HealthTarget(scope, candidate.connection["connection_id"])
        if scope == "credential":
            return HealthTarget(scope, candidate.credential["credential_id"])
        if scope == "credential_deployment":
            return HealthTarget(
                scope,
                candidate.credential["credential_id"],
                candidate.deployment["deployment_id"],
            )
        return HealthTarget("deployment", candidate.deployment["deployment_id"])

    def _prune_locked(self, now: float) -> int:
        expired = [
            target for target, entry in self._entries.items()
            if entry.expires_at <= now
        ]
        for target in expired:
            self._entries.pop(target, None)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return len(expired)

    def record_failure(
        self,
        target: HealthTarget,
        *,
        reason: str,
        cooldown_seconds: float | None = None,
    ) -> None:
        """Learn one failure episode; only a repeat after its cooldown backs off.

        Observations while the target still cools (credential-recovery retries,
        a demoted route re-admitted because nothing else qualifies, concurrent
        requests) are the same outage: they neither escalate nor extend it.
        """
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            previous = self._entries.get(target)
            if (previous is not None and cooldown_seconds is None
                    and previous.unavailable_until > now):
                self._entries.move_to_end(target)
                return
            self._entries.pop(target, None)
            failures = 1 if previous is None else min(previous.failures + 1, 16)
            base = {
                "deployment": 60.0,
                "connection": 30.0,
                "credential": 300.0,
                "credential_deployment": 600.0,
            }.get(target.scope, 60.0)
            cooldown = (
                min(max(float(cooldown_seconds), 0.0), self.ttl_seconds)
                if cooldown_seconds is not None
                else min(base * (2 ** (failures - 1)), self.ttl_seconds)
            )
            self._entries[target] = _HealthEntry(
                failures=failures,
                unavailable_until=now + cooldown,
                expires_at=now + self.ttl_seconds,
                reason=str(reason or "failure")[:256],
            )
            self._prune_locked(now)

    def record_candidate_failure(
        self,
        candidate,
        *,
        status_code: int | None = None,
        kind: str = "",
        reason: str = "",
        cooldown_seconds: float | None = None,
    ) -> HealthTarget:
        target = self.target_for_failure(
            candidate, status_code=status_code, kind=kind)
        self.record_failure(
            target,
            reason=reason or kind or str(status_code or "failure"),
            cooldown_seconds=cooldown_seconds,
        )
        return target

    def record_success(self, candidate) -> None:
        """Clear only the exact successful path; unrelated faults survive."""
        targets = (
            HealthTarget("deployment", candidate.deployment["deployment_id"]),
            HealthTarget("connection", candidate.connection["connection_id"]),
            HealthTarget("credential", candidate.credential["credential_id"]),
            HealthTarget(
                "credential_deployment",
                candidate.credential["credential_id"],
                candidate.deployment["deployment_id"],
            ),
        )
        with self._lock:
            for target in targets:
                self._entries.pop(target, None)

    def clear_target(self, target: HealthTarget) -> None:
        """Forget one entity after it was proven healthy again."""
        with self._lock:
            self._entries.pop(target, None)

    def target_unavailable(self, target: HealthTarget) -> bool:
        """Whether one entity is still inside its learned cooldown."""
        now = self._clock()
        with self._lock:
            entry = self._entries.get(target)
            return entry is not None and entry.unavailable_until > now

    def candidate_state(self, candidate) -> tuple[bool, float, list[str]]:
        now = self._clock()
        targets = (
            HealthTarget("deployment", candidate.deployment["deployment_id"]),
            HealthTarget("connection", candidate.connection["connection_id"]),
            HealthTarget("credential", candidate.credential["credential_id"]),
            HealthTarget(
                "credential_deployment",
                candidate.credential["credential_id"],
                candidate.deployment["deployment_id"],
            ),
        )
        penalty = 0.0
        reasons: list[str] = []
        unavailable = False
        with self._lock:
            self._prune_locked(now)
            for target in targets:
                entry = self._entries.get(target)
                if entry is None:
                    continue
                self._entries.move_to_end(target)
                penalty += float(entry.failures)
                if entry.unavailable_until > now:
                    unavailable = True
                    reasons.append(f"{target.scope}:{entry.reason}")
        return unavailable, penalty, reasons

    def snapshot(self, *, limit: int = 256) -> dict:
        now = self._clock()
        with self._lock:
            pruned = self._prune_locked(now)
            rows = list(self._entries.items())[-max(1, min(int(limit), 256)):]
            return {
                "entries": [
                    {
                        "scope": target.scope,
                        "entity_id": target.entity_id,
                        "related_id": target.related_id,
                        "failures": entry.failures,
                        "cooldown_remaining": max(0.0, entry.unavailable_until - now),
                        "expires_in": max(0.0, entry.expires_at - now),
                        "reason": entry.reason,
                    }
                    for target, entry in rows
                ],
                "count": len(self._entries),
                "capacity": self.max_entries,
                "ttl_seconds": self.ttl_seconds,
                "pruned": pruned,
            }


_owner_registries: OrderedDict[tuple[int, str], RouteHealthRegistry] = OrderedDict()
_owner_registries_lock = threading.Lock()


def _owner_registry_key(owner_user_id: int, tenant_id: str | None) -> tuple[int, str]:
    from lib.identity import require_user_id
    return (
        require_user_id(owner_user_id, context="route health owner"),
        str(tenant_id or "").strip(),
    )


def owner_health_registry(
    owner_user_id: int, tenant_id: str | None = "",
) -> RouteHealthRegistry:
    """Return one owner's process-local route health, creating it on demand.

    The least recently used owner table is dropped beyond
    ``MAX_OWNER_HEALTH_REGISTRIES``; losing it only forgets cooldowns.
    """
    key = _owner_registry_key(owner_user_id, tenant_id)
    with _owner_registries_lock:
        registry = _owner_registries.get(key)
        if registry is None:
            registry = _owner_registries[key] = RouteHealthRegistry()
            while len(_owner_registries) > MAX_OWNER_HEALTH_REGISTRIES:
                _owner_registries.popitem(last=False)
        else:
            _owner_registries.move_to_end(key)
        return registry


def existing_owner_health_registry(
    owner_user_id: int, tenant_id: str | None = "",
) -> RouteHealthRegistry | None:
    """Read-only lookup: probing health never allocates an owner table."""
    key = _owner_registry_key(owner_user_id, tenant_id)
    with _owner_registries_lock:
        registry = _owner_registries.get(key)
        if registry is not None:
            _owner_registries.move_to_end(key)
        return registry


def owner_health_learned() -> bool:
    """Cheap hot-path guard: False until any owner recorded route health."""
    return bool(_owner_registries)


def reset_owner_health_registries() -> None:
    """Forget every learned owner table (process restart equivalent)."""
    with _owner_registries_lock:
        _owner_registries.clear()


__all__ = [
    "CREDENTIAL_SOURCE_UNAVAILABLE",
    "HealthTarget",
    "MAX_OWNER_HEALTH_REGISTRIES",
    "RouteHealthRegistry",
    "classify_failure",
    "existing_owner_health_registry",
    "owner_health_learned",
    "owner_health_registry",
    "reset_owner_health_registries",
]

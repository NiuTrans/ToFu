"""Track owned SSE connection lifetimes with a probe-derived admission cap.

Routes supply an authenticated principal and release their opaque token on
close. The default per-principal cap derives from the launch-time resource
probe (``TOFU_MAX_SSE_PER_PRINCIPAL``); ``cap=0`` preserves the explicit
unbounded escape hatch. Reconnect ownership and generation fencing belong to
ConversationWakeBroker.
"""
from __future__ import annotations
import threading
import uuid


class SSELimiter:
    """Per-principal connection accounting with a bounded admission cap."""
    def __init__(self, cap: int | None = None):
        if cap is None:
            from runtime_guards import resolve_resource_budget
            cap = resolve_resource_budget(
                'TOFU_MAX_SSE_PER_PRINCIPAL', minimum=1, maximum=128)
        self.cap = max(0, int(cap))
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}
        self._active_by_principal: dict[str, int] = {}

    @property
    def refresh_interval_seconds(self) -> float:
        return 60.0  # compatibility with existing route keepalive scheduling

    def try_acquire(self, principal: str) -> str | None:
        if not isinstance(principal, str) or not principal:
            raise ValueError('SSE principal must be explicit')
        token = uuid.uuid4().hex
        with self._lock:
            if self.cap > 0 and self._active_by_principal.get(principal, 0) >= self.cap:
                return None
            self._held[token] = principal
            self._active_by_principal[principal] = self._active_by_principal.get(principal, 0) + 1
        return token

    def refresh(self, token: str) -> None:
        """Connection lifetime is owned by the route, never an expiring lease."""

    def release(self, token: str) -> None:
        with self._lock:
            principal = self._held.pop(token, None)
            if principal is None:
                return
            remaining = self._active_by_principal[principal] - 1
            if remaining:
                self._active_by_principal[principal] = remaining
            else:
                del self._active_by_principal[principal]

    def active(self, principal: str) -> int:
        with self._lock:
            return self._active_by_principal.get(principal, 0)

    def stats(self) -> dict:
        return {'cap': self.cap}


limiter = SSELimiter()
__all__ = ['SSELimiter', 'limiter']

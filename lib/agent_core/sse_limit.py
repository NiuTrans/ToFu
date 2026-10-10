"""Track owned SSE connection lifetimes without connection admission quotas.

Routes supply an authenticated principal and release their opaque token on
close. No shared capacity store or lease heartbeat participates in admission;
reconnect ownership and generation fencing belong to ConversationWakeBroker.
"""
from __future__ import annotations
import threading
import uuid


class SSELimiter:
    """Connection accounting; the historical cap input cannot refuse streams."""
    def __init__(self, cap: int | None = None):
        self.cap = 0
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}
        self._active_by_principal: dict[str, int] = {}

    @property
    def refresh_interval_seconds(self) -> float:
        return 60.0  # compatibility with existing route keepalive scheduling

    def try_acquire(self, principal: str) -> str:
        if not isinstance(principal, str) or not principal:
            raise ValueError('SSE principal must be explicit')
        token = uuid.uuid4().hex
        with self._lock:
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
        return {'cap': 0}


limiter = SSELimiter()
__all__ = ['SSELimiter', 'limiter']

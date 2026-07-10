"""lib/agent_core/sse_limit.py — Per-principal concurrent-SSE cap.

A single Hypercorn process serves every SSE chat stream as a long-lived
connection. With no per-principal ceiling, one client (or one abusive IP in
open mode) can open an unbounded number of streams and exhaust the process —
the classic way a single actor darks an ASGI server.

**Backed by the shared lease store (Build Order step 2).** The cap is no
longer a private in-process dict — it re-keys onto
``lib.runtime_state_store`` via the ATOMIC bounded ``acquire_slot`` primitive.
Under the default ``inproc`` backend the behaviour is byte-equivalent to the
old dict (same process = authoritative count); under
``TOFU_RUNTIME_STATE_BACKEND=redis`` the cap becomes ``N``-invariant across
replicas and a crashed replica's stream slots reclaim by lease TTL. The atomic
acquire means concurrent stream opens can NEVER overshoot the cap (no
check-then-act race).

Contract (token-based, so each stream owns a distinct slot):
  * ``try_acquire(principal)`` → an opaque ``token`` string to pass to
    ``release`` when the stream ends, or ``None`` when at capacity (caller
    returns 429 + Retry-After).
  * ``release(token)`` → free the slot; MUST run in a ``finally`` so a
    dropped / aborted / errored stream can never leak a slot. The eager
    release is the normal path; the lease TTL is the crash-only backstop.
  * cap via ``TOFU_MAX_SSE_PER_PRINCIPAL`` (default 12); ``0`` disables.

The lease TTL for a stream slot is generous (streams can last up to the
2h SSE ceiling) — the heartbeat that keeps a living stream's slot alive is
the SSE keepalive loop refreshing it (wired in the route). TTL only reclaims a
slot whose owner crashed.
"""

from __future__ import annotations

import os
import uuid

from lib.log import get_logger

logger = get_logger(__name__)

_KIND = 'sse'


def _default_cap() -> int:
    """Concurrent-SSE ceiling per principal, from env with a safe default.

    Default 12 comfortably covers a power user with several tabs + a
    reconnecting mobile client, while still bounding an abuser to a dozen
    streams. Override via ``TOFU_MAX_SSE_PER_PRINCIPAL``; ``0`` disables.
    """
    try:
        n = int(os.environ.get('TOFU_MAX_SSE_PER_PRINCIPAL', '') or '12')
    except (ValueError, TypeError) as e:
        logger.debug('[SSELimit] TOFU_MAX_SSE_PER_PRINCIPAL parse failed, using default: %s', e)
        n = 12
    return max(0, n)


def _slot_ttl() -> float:
    """Lease TTL for a stream slot (seconds).

    Must exceed the max legit stream lifetime UNLESS the route refreshes it;
    the SSE loop refreshes via ``refresh(token)`` on each keepalive, so a
    living stream never expires. Default 300s (5 min) — a stream idle longer
    than that with no keepalive is treated as dead and its slot reclaims.
    Override via ``TOFU_SSE_SLOT_TTL``.
    """
    try:
        n = float(os.environ.get('TOFU_SSE_SLOT_TTL', '') or '300')
    except (ValueError, TypeError) as e:
        logger.debug('[SSELimit] TOFU_SSE_SLOT_TTL parse failed, using default: %s', e)
        n = 300.0
    return max(1.0, n)


class SSELimiter:
    """Bounds concurrent open SSE streams per principal via the shared store."""

    def __init__(self, cap: int | None = None):
        self.cap = _default_cap() if cap is None else max(0, cap)
        self._ttl = _slot_ttl()

    def _store(self):
        from lib.runtime_state_store import get_store
        return get_store()

    def try_acquire(self, principal: str) -> str | None:
        """Atomically reserve a stream slot for ``principal``.

        Returns an opaque token (pass to :meth:`release` / :meth:`refresh`) on
        success, or ``None`` when the principal is at capacity. When the cap is
        disabled (``0``) a token is still returned so ``release`` is uniform.
        """
        # Unique per-stream slot key under the principal's count prefix.
        prefix = f'{principal}::'
        slot_key = f'{prefix}{uuid.uuid4().hex}'
        if self.cap <= 0:
            self._store().acquire_lease(_KIND, slot_key, self._ttl)
            return slot_key
        ok = self._store().acquire_slot(
            _KIND, slot_key, limit=self.cap, ttl=self._ttl, count_prefix=prefix)
        return slot_key if ok else None

    @staticmethod
    def _prefix_of(token: str) -> str:
        """Recover the count_prefix (``<principal>::``) from a slot token so
        release/refresh target the same (kind, count_prefix) cap as acquire."""
        i = token.rfind('::')
        return token[:i + 2] if i >= 0 else ''

    def refresh(self, token: str) -> None:
        """Re-arm a held slot (SSE keepalive heartbeat) so a living stream's
        slot never expires. Re-acquiring the SAME member ZADD-refreshes its
        deadline score (no double-count); under the disabled cap it's a lease
        refresh."""
        if not token:
            return
        if self.cap <= 0:
            self._store().refresh_lease(_KIND, token, self._ttl)
            return
        self._store().acquire_slot(_KIND, token, limit=self.cap, ttl=self._ttl,
                                   count_prefix=self._prefix_of(token))

    def release(self, token: str) -> None:
        """Free a slot. Idempotent; safe on a None/empty token."""
        if not token:
            return
        if self.cap <= 0:
            self._store().release_lease(_KIND, token)
            return
        self._store().release_slot(_KIND, token, self._prefix_of(token))

    def active(self, principal: str) -> int:
        return self._store().count_slots(_KIND, f'{principal}::')

    def stats(self) -> dict:
        return {'cap': self.cap}


# Process-global limiter used by the SSE stream route.
limiter = SSELimiter()


__all__ = ['SSELimiter', 'limiter']

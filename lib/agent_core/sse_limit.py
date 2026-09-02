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
  * cap via the launch-probed ``TOFU_MAX_SSE_PER_PRINCIPAL`` budget. Invalid,
    zero, and huge environment values fall back/clamp instead of disabling the
    resident-connection ceiling.

The lease TTL for a stream slot is generous (streams can last up to the
2h SSE ceiling) — the heartbeat that keeps a living stream's slot alive is
the SSE keepalive loop refreshing it (wired in the route). TTL only reclaims a
slot whose owner crashed.
"""

from __future__ import annotations

import os
import uuid

from lib.log import get_logger
from runtime_guards import resolve_resource_budget

logger = get_logger(__name__)

_KIND = 'sse'


def _default_cap() -> int:
    """Launch-probed concurrent-SSE ceiling per principal.

    The 8 GiB reference machine resolves to 12: enough for several tabs and
    direct API streams without allowing proxy/socket residency to grow with
    reconnect history. Distributed deployments resolve to 64; all overrides
    retain the hard 128-stream ceiling.
    """
    return resolve_resource_budget(
        'TOFU_MAX_SSE_PER_PRINCIPAL', minimum=1, maximum=128)


def _slot_ttl() -> float:
    """Lease TTL for a stream slot (seconds).

    Must exceed the max legit stream lifetime UNLESS the route refreshes it;
    the SSE loop refreshes via ``refresh(token)`` on each keepalive, so a
    living stream never expires. Default 300s (5 min) — a stream idle longer
    than that with no keepalive is treated as dead and its slot reclaims.
    Override via ``TOFU_SSE_SLOT_TTL``; values are clamped to 45..3600 so a
    normal 15-second heartbeat can refresh before expiry and crash residue
    still has a finite reclaim window.
    """
    try:
        n = float(os.environ.get('TOFU_SSE_SLOT_TTL', '') or '300')
    except (ValueError, TypeError) as e:
        logger.debug('[SSELimit] TOFU_SSE_SLOT_TTL parse failed, using default: %s', e)
        n = 300.0
    return max(45.0, min(3600.0, n))


class SSELimiter:
    """Bounds concurrent open SSE streams per principal via the shared store."""

    def __init__(self, cap: int | None = None):
        self.cap = (
            _default_cap() if cap is None else max(1, min(128, int(cap))))
        self._ttl = _slot_ttl()

    @property
    def refresh_interval_seconds(self) -> float:
        """Maximum wait between living-stream lease refreshes."""
        return max(1.0, min(60.0, self._ttl / 3.0))

    def _store(self):
        from lib.runtime_state_store import get_store
        return get_store()

    def try_acquire(self, principal: str) -> str | None:
        """Atomically reserve a stream slot for ``principal``.

        Returns an opaque token (pass to :meth:`release` / :meth:`refresh`) on
        success, or ``None`` when the principal is at capacity.
        """
        # Unique per-stream slot key under the principal's count prefix.
        prefix = f'{principal}::'
        slot_key = f'{prefix}{uuid.uuid4().hex}'
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
        deadline score without double-counting."""
        if not token:
            return
        self._store().acquire_slot(_KIND, token, limit=self.cap, ttl=self._ttl,
                                   count_prefix=self._prefix_of(token))

    def release(self, token: str) -> None:
        """Free a slot. Idempotent; safe on a None/empty token."""
        if not token:
            return
        self._store().release_slot(_KIND, token, self._prefix_of(token))

    def active(self, principal: str) -> int:
        return self._store().count_slots(_KIND, f'{principal}::')

    def stats(self) -> dict:
        return {'cap': self.cap}


# Process-global limiter used by the SSE stream route.
limiter = SSELimiter()


__all__ = ['SSELimiter', 'limiter']

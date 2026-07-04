"""lib/agent_core/principal.py — Resolve a stable per-request principal key.

Several backpressure caps (per-principal concurrent-SSE semaphore, per-IP
token-bucket throttle for the open-mode synthetic context) need a single,
consistent notion of "who is this request" to key their state on. This module
is that single source of truth so the caps agree on the identity.

Resolution order (most-specific → least):
  1. ``user_id``  — the owning user in multi-user mode (survives key rotation).
  2. ``key_id``   — the API key in private mode.
  3. client IP    — the direct socket peer, for the open-mode synthetic
                    context (no real credential). NOT ``X-Forwarded-For``
                    (a remote client can spoof that); mirrors
                    ``routes.api_v1.auth._remote_is_loopback``'s discipline.

The tunnel-token / cookie-UI paths resolve to their ``key_id`` when present,
else the IP — they are the operator's own local surface and are effectively
uncapped in practice (loopback IP, one principal).
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _client_ip() -> str:
    """Best-effort direct socket peer for the current request.

    Uses ``request.remote_addr`` (the real peer), never a spoofable
    forwarded header. Returns ``'unknown'`` when no request context / no
    peer info is available so callers still get a stable (shared) bucket
    rather than crashing.
    """
    try:
        from flask import request
        addr = (request.remote_addr or '').strip()
    except Exception as e:  # no request context / import edge
        logger.debug('[principal] client_ip unavailable: %s', e)
        return 'unknown'
    if not addr:
        return 'unknown'
    # Quart's in-process test client reports '<local>'; keep it as-is (it is
    # a stable, single principal for tests).
    return addr.split('%', 1)[0]  # strip IPv6 zone id


def principal_key(auth_ctx=None) -> str:
    """Return a stable ``"<kind>:<id>"`` key identifying the request principal.

    ``auth_ctx`` is the resolved :class:`lib.api_keys.AuthContext` (or None).
    The prefix keeps namespaces from colliding (a key_id can never masquerade
    as an IP). Callers use the returned string as a dict key.
    """
    if auth_ctx is not None:
        uid = getattr(auth_ctx, 'user_id', '') or ''
        if uid:
            return f'user:{uid}'
        kid = getattr(auth_ctx, 'key_id', '') or ''
        if kid:
            return f'key:{kid}'
    return f'ip:{_client_ip()}'


__all__ = ['principal_key']

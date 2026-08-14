"""lib/rate_limit_api.py — Token-bucket rate limiter for the public API.

Two independent buckets per key:
  - **rpm**: requests per minute   (refill rate = limit/60 tokens/sec)
  - **tpd**: tokens per day        (refill rate = limit/86400 tokens/sec)

Both buckets are checked on every request that has been authenticated
via an API key. ``TUNNEL_TOKEN``-authenticated requests bypass the
limiter entirely (the UI is local; the user already has cookie/header
access and we don't want to disrupt the browser).

Usage
-----

    from lib.rate_limit_api import check_request, record_tokens

    # In auth middleware (after token validation):
    decision = check_request(auth_ctx, request_cost=1)
    if not decision.allowed:
        return rate_limit_response(decision)

    # Apply standard headers to the eventual response:
    apply_headers(response, decision)

    # Optionally, after an LLM call, record the actual token usage:
    record_tokens(auth_ctx.key_id, prompt_tokens + completion_tokens)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)


@dataclass
class _Bucket:
    capacity: float
    tokens: float
    refill_rate: float  # tokens per second
    last_refill: float

    def refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def try_consume(self, n: float, now: float) -> bool:
        self.refill(now)
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def consume_force(self, n: float, now: float) -> None:
        """Consume tokens even if the bucket goes negative — used for actual
        post-hoc token usage where we already issued the LLM call."""
        self.refill(now)
        self.tokens -= n

    def retry_after(self, n: float, now: float) -> float:
        """Seconds until the bucket has ``n`` tokens available."""
        self.refill(now)
        if self.tokens >= n:
            return 0.0
        if self.refill_rate <= 0:
            return float('inf')
        return (n - self.tokens) / self.refill_rate


@dataclass
class RateDecision:
    allowed: bool
    reason: str = ''       # 'rpm' | 'tpd' | ''
    retry_after_s: float = 0.0
    rpm_limit: int = 0
    rpm_remaining: int = 0
    tpd_limit: int = 0
    tpd_remaining: int = 0


# ── Storage: per-key bucket pair, keyed by key_id ──
_lock = threading.Lock()
_state: dict[str, dict] = {}

# ── Open-mode per-IP throttle ──
# Open mode (esp. TOFU_OPEN_MODE_ALLOW_REMOTE=1) hands requests a synthetic
# admin context with NO real principal to key a per-key bucket on. Without a
# cap, open+remote is simultaneously unauthenticated AND unthrottled — a single
# IP can hammer the expensive LLM/search/browser/PDF endpoints. We enforce a
# coarse per-IP RPM ceiling. Crucially the counter is delegated to the SHARED
# lib.rate_limit_store seam (record_and_check), NOT a fresh in-process dict, so
# under TOFU_RATE_LIMIT_BACKEND=db the cap holds ACROSS replicas (behind an
# N-replica load balancer an in-process cap would silently become rpm×N — the
# exact "cap scales with replica count" failure the shared store was built to
# prevent). Cap resolution (see _open_mode_rpm): an explicit
# TOFU_OPEN_MODE_RPM always wins; when UNSET the cap auto-arms at
# _OPEN_MODE_REMOTE_RPM only if remote open-mode peers are admitted
# (TOFU_OPEN_MODE_ALLOW_REMOTE=1) — a loopback-only install ships uncapped,
# because the only IPs the bucket could ever see are the operator's own.
_OPEN_MODE_ENDPOINT = 'open_mode'  # (endpoint, ip) key namespace in the store

# Auto-armed ceiling for the remote-open configuration. Sized against
# EXPENSIVE calls only — ambient poll/status reads are exempt (see
# _OPEN_MODE_EXEMPT_SUBSTRINGS) and a human-driven UI issues single-digit
# expensive calls per minute, so 120 leaves real headroom for a small team
# sharing one egress IP behind NAT.
_OPEN_MODE_REMOTE_RPM = 120

# Ambient poll/status reads that NEVER consume the open-mode budget.
#
# The cap exists to stop a remote IP hammering EXPENSIVE surfaces (LLM /
# chat / search / generate). Behind a reverse proxy (VS Code port-forward,
# ngrok, …) every UI client shares ONE direct peer IP, so the operator's own
# ambient polling is indistinguishable from that hammer by IP alone: the UI
# probes browser/desktop status every ~3s and drives the TaskRuntime poll
# seam at 1.2–2.5s while a job runs — the default 120/min budget was gone
# before any real interaction (owner incident 2026-08-14: two filter clicks
# in the knowledge panel = 429). These paths are cheap read-only probes by
# construction (all /poll routes are GET cursor reads), so they are exempt.
# The exemption is path-scoped on purpose: sibling mutation paths under the
# same prefix (e.g. /api/v1/browser/commands) stay capped. Operators can
# extend the list via TOFU_OPEN_MODE_EXEMPT_PATHS (comma-separated path
# substrings) without a redeploy.
_OPEN_MODE_EXEMPT_SUBSTRINGS = (
    '/api/v1/browser/status',
    '/api/v1/desktop/status',
    '/api/v1/dispatch/model-health',
    '/poll/',
)


def _request_path_or_none() -> str:
    """Best-effort request path; '' when no request context is active."""
    try:
        from quart import request
        return request.path or ''
    except Exception as e:
        logger.debug('[RateLimit] request path unavailable: %s', e)
        return ''


def _open_mode_extra_exempt() -> tuple:
    """Operator-supplied extra exempt substrings (env, live-read)."""
    import os
    raw = os.environ.get('TOFU_OPEN_MODE_EXEMPT_PATHS', '') or ''
    return tuple(p.strip() for p in raw.split(',') if p.strip())


def _open_mode_path_exempt(path: str) -> bool:
    """Whether ``path`` is an ambient read exempt from the open-mode bucket."""
    if path.endswith('/poll'):
        return True
    subs = _OPEN_MODE_EXEMPT_SUBSTRINGS + _open_mode_extra_exempt()
    return any(s in path for s in subs)


def _open_mode_rpm() -> int:
    """Resolve the per-IP RPM ceiling for open-mode requests.

    An explicit ``TOFU_OPEN_MODE_RPM`` always wins, in both directions: a
    positive value arms the cap even on loopback-only installs (operator
    wants a dam), ``0`` disarms it even with remote peers admitted. When
    UNSET the cap AUTO-ARMS only when the operator opted into remote
    open-mode peers (``TOFU_OPEN_MODE_ALLOW_REMOTE``, via
    ``lib.auth_mode.open_mode_allows_remote``) — without that opt-in the
    synthetic grant only ever reaches loopback peers, so the bucket's only
    possible tenant is the operator's own UI tabs and pollers sharing one
    bucket: the cap protects nothing and purely throttles the owner (owner
    incident 2026-08-14: ambient UI polling ate 120/min → 606 self-429s/day).
    Behind a same-host tunnel the cap has no discriminating power either
    (every public request presents as loopback) — the documented protection
    there is TUNNEL_TOKEN / private mode, never IP throttling.
    """
    import os
    raw = (os.environ.get('TOFU_OPEN_MODE_RPM', '') or '').strip()
    if raw:
        try:
            return max(0, int(raw))
        except (ValueError, TypeError) as e:
            logger.debug('[RateLimit] TOFU_OPEN_MODE_RPM=%r parse failed, '
                         'falling back to the auto default: %s', raw, e)
    from lib.auth_mode import open_mode_allows_remote
    return _OPEN_MODE_REMOTE_RPM if open_mode_allows_remote() else 0


def _state_for(key_id: str, rpm_limit: int, tpd_limit: int) -> dict:
    """Return / lazily create the bucket state for this key."""
    now = time.time()
    entry = _state.get(key_id)
    if entry is None:
        entry = {
            'rpm': _Bucket(
                capacity=float(rpm_limit) if rpm_limit > 0 else 0.0,
                tokens=float(rpm_limit) if rpm_limit > 0 else 0.0,
                refill_rate=(rpm_limit / 60.0) if rpm_limit > 0 else 0.0,
                last_refill=now,
            ),
            'tpd': _Bucket(
                capacity=float(tpd_limit) if tpd_limit > 0 else 0.0,
                tokens=float(tpd_limit) if tpd_limit > 0 else 0.0,
                refill_rate=(tpd_limit / 86400.0) if tpd_limit > 0 else 0.0,
                last_refill=now,
            ),
        }
        _state[key_id] = entry
        return entry
    # Reconfigure if the limits changed (admin updated the key).
    rpm = entry['rpm']
    if rpm.capacity != rpm_limit:
        rpm.capacity = float(rpm_limit) if rpm_limit > 0 else 0.0
        rpm.refill_rate = (rpm_limit / 60.0) if rpm_limit > 0 else 0.0
        rpm.tokens = min(rpm.tokens, rpm.capacity)
    tpd = entry['tpd']
    if tpd.capacity != tpd_limit:
        tpd.capacity = float(tpd_limit) if tpd_limit > 0 else 0.0
        tpd.refill_rate = (tpd_limit / 86400.0) if tpd_limit > 0 else 0.0
        tpd.tokens = min(tpd.tokens, tpd.capacity)
    return entry


def check_request(auth_ctx, *, request_cost: int = 1) -> RateDecision:
    """Pre-flight check: consume one RPM token. Returns RateDecision.

    ``request_cost`` lets a route declare it costs more than one slot
    (e.g. parallel batch endpoint). TPD is NOT decremented here — call
    ``record_tokens()`` after the upstream LLM returns its usage.
    """
    if auth_ctx is not None and getattr(auth_ctx, 'via_open_mode', False):
        # Open-mode synthetic context: no real principal, so key a coarse
        # per-IP RPM bucket instead of bypassing entirely. This closes the
        # "open+remote = unauthenticated AND unthrottled" hole. The tunnel /
        # cookie-UI paths (below) remain uncapped — they are the operator's
        # own local surface.
        return check_open_mode_request(request_cost=request_cost)
    if (auth_ctx is None or auth_ctx.via_tunnel_token
            or not auth_ctx.key_id):
        # Bypass for unauthenticated (rejected upstream) and the UI cookie /
        # tunnel path (no real principal to rate-limit). Anything that gets
        # here is "no limit configured".
        return RateDecision(allowed=True)
    rpm = max(0, int(auth_ctx.rate_limit_rpm or 0))
    tpd = max(0, int(auth_ctx.rate_limit_tpd or 0))
    if rpm == 0 and tpd == 0:
        return RateDecision(allowed=True, rpm_limit=0, tpd_limit=0)
    now = time.time()
    with _lock:
        entry = _state_for(auth_ctx.key_id, rpm, tpd)
        rpm_b: _Bucket = entry['rpm']
        tpd_b: _Bucket = entry['tpd']
        if rpm > 0 and not rpm_b.try_consume(request_cost, now):
            wait = rpm_b.retry_after(request_cost, now)
            return RateDecision(
                allowed=False, reason='rpm', retry_after_s=wait,
                rpm_limit=rpm, rpm_remaining=int(max(0, rpm_b.tokens)),
                tpd_limit=tpd, tpd_remaining=int(max(0, tpd_b.tokens)),
            )
        if tpd > 0 and tpd_b.tokens <= 0:
            wait = tpd_b.retry_after(1, now)
            # Refund the RPM token we just consumed.
            if rpm > 0:
                rpm_b.tokens = min(rpm_b.capacity, rpm_b.tokens + request_cost)
            return RateDecision(
                allowed=False, reason='tpd', retry_after_s=wait,
                rpm_limit=rpm, rpm_remaining=int(max(0, rpm_b.tokens)),
                tpd_limit=tpd, tpd_remaining=0,
            )
        return RateDecision(
            allowed=True,
            rpm_limit=rpm, rpm_remaining=int(max(0, rpm_b.tokens)),
            tpd_limit=tpd, tpd_remaining=int(max(0, tpd_b.tokens)),
        )


def check_open_mode_request(*, request_cost: int = 1,
                            client_ip: str | None = None) -> RateDecision:
    """Per-IP RPM check for the open-mode synthetic context.

    Keyed by the direct client IP (never a spoofable forwarded header) and
    delegated to the SHARED ``lib.rate_limit_store`` counter (a sliding
    60s window), so under ``TOFU_RATE_LIMIT_BACKEND=db`` the cap holds across
    replicas rather than becoming ``rpm × N``. When the resolved ceiling is
    0 (explicit ``TOFU_OPEN_MODE_RPM=0``, or unset with no remote peers —
    see :func:`_open_mode_rpm`) the cap is disabled and every request is
    allowed.
    ``request_cost`` collapses to one recorded event per request (the store is
    event-count based, not token-bucket); a cost > 1 is treated as a single
    slot — acceptable for the coarse open-mode ceiling. ``client_ip`` is
    resolved from the request when not supplied.

    Ambient poll/status reads (UI heartbeat probes — browser/desktop
    status, the GET ``/poll`` cursor seam, model-health rows) are EXEMPT
    from the bucket: behind a reverse proxy all clients share one peer IP,
    and the operator's own UI polling must not eat the anti-hammer budget
    (see ``_OPEN_MODE_EXEMPT_SUBSTRINGS``; extend via
    ``TOFU_OPEN_MODE_EXEMPT_PATHS``). Static assets and ``/api/health``
    never reach this check at all (public-path short-circuit upstream).

    Fail-open: the store itself degrades to ``(True, 0)`` on any DB error, so
    a throttle failure never takes down the server.
    """
    rpm = _open_mode_rpm()
    if rpm <= 0:
        return RateDecision(allowed=True)
    path = _request_path_or_none()
    if path and _open_mode_path_exempt(path):
        # Ambient status/poll reads never consume the anti-hammer budget —
        # they are the operator's own UI heartbeat, not a hammering signal.
        logger.debug('[RateLimit] open-mode ambient read exempt: %s', path)
        return RateDecision(allowed=True)
    if client_ip is None:
        try:
            from quart import request
            client_ip = (request.remote_addr or 'unknown').split('%', 1)[0]
        except Exception as e:
            logger.debug('[RateLimit] open-mode client_ip unavailable: %s', e)
            client_ip = 'unknown'
    try:
        from lib.rate_limit_store import get_store
        allowed, count = get_store().record_and_check(
            _OPEN_MODE_ENDPOINT, client_ip, limit=rpm, per_seconds=60)
    except Exception as e:
        # Defence in depth — the store is already fail-open, but never let a
        # throttle-path error bubble into the request.
        logger.warning('[RateLimit] open-mode store check failed (%s) — '
                       'failing open', e)
        return RateDecision(allowed=True, rpm_limit=rpm, rpm_remaining=rpm)
    remaining = max(0, rpm - count)
    if not allowed:
        logger.warning('[RateLimit] open-mode IP %s throttled '
                       '(rpm cap=%d, count=%d)', client_ip, rpm, count)
        # Sliding-window store has no exact refill time; advise a full window.
        return RateDecision(allowed=False, reason='rpm', retry_after_s=60.0,
                            rpm_limit=rpm, rpm_remaining=0)
    return RateDecision(allowed=True, rpm_limit=rpm, rpm_remaining=remaining)


def record_tokens(key_id: str, n_tokens: int, *, rpm_limit: int = 0,
                   tpd_limit: int = 0) -> None:
    """After an LLM call completes, deduct its actual usage from the TPD bucket.

    Called from chat / agent routes once they know the upstream usage.
    Negative bucket values are allowed (we already paid for the call) —
    they recover at the bucket's refill rate.
    """
    if not key_id or n_tokens <= 0:
        return
    now = time.time()
    with _lock:
        entry = _state.get(key_id)
        if entry is None:
            if tpd_limit <= 0 and rpm_limit <= 0:
                return
            entry = _state_for(key_id, rpm_limit, tpd_limit)
        entry['tpd'].consume_force(n_tokens, now)


def apply_headers(response, decision: RateDecision) -> None:
    """Attach standard rate-limit headers to a Flask/Quart response."""
    if not decision or decision.rpm_limit <= 0 and decision.tpd_limit <= 0:
        return
    try:
        if decision.rpm_limit > 0:
            response.headers['X-RateLimit-Limit-Requests'] = str(decision.rpm_limit)
            response.headers['X-RateLimit-Remaining-Requests'] = str(decision.rpm_remaining)
        if decision.tpd_limit > 0:
            response.headers['X-RateLimit-Limit-Tokens'] = str(decision.tpd_limit)
            response.headers['X-RateLimit-Remaining-Tokens'] = str(decision.tpd_remaining)
        if not decision.allowed and decision.retry_after_s > 0:
            response.headers['Retry-After'] = str(int(decision.retry_after_s) + 1)
    except Exception as e:
        logger.debug('[RateLimit] header injection failed: %s', e)


__all__ = ['RateDecision', 'check_request', 'check_open_mode_request',
           'record_tokens', 'apply_headers']

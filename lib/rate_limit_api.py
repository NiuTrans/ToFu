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
# prevent). Cap via TOFU_OPEN_MODE_RPM (default 120/min); 0 disables.
_OPEN_MODE_ENDPOINT = 'open_mode'  # (endpoint, ip) key namespace in the store


def _open_mode_rpm() -> int:
    """Per-IP requests-per-minute ceiling for open-mode requests, from env."""
    import os
    try:
        n = int(os.environ.get('TOFU_OPEN_MODE_RPM', '') or '120')
    except (ValueError, TypeError) as e:
        logger.debug('[RateLimit] TOFU_OPEN_MODE_RPM parse failed, using default: %s', e)
        n = 120
    return max(0, n)


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
    replicas rather than becoming ``rpm × N``. When ``TOFU_OPEN_MODE_RPM`` is 0
    the cap is disabled and every request is allowed (legacy behaviour).
    ``request_cost`` collapses to one recorded event per request (the store is
    event-count based, not token-bucket); a cost > 1 is treated as a single
    slot — acceptable for the coarse open-mode ceiling. ``client_ip`` is
    resolved from the request when not supplied.

    Fail-open: the store itself degrades to ``(True, 0)`` on any DB error, so
    a throttle failure never takes down the server.
    """
    rpm = _open_mode_rpm()
    if rpm <= 0:
        return RateDecision(allowed=True)
    if client_ip is None:
        try:
            from flask import request
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

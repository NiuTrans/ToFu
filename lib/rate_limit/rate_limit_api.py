"""lib/rate_limit/rate_limit_api.py — Token-bucket rate limiter for the public API.

Only an explicitly configured tokens-per-day spending allowance is checked.
Request throughput has no RPM bucket. Legacy RPM fields and request-cost
arguments are accepted but ignored, including on pre-existing credentials.
Open-mode peer admission remains owned by the authentication boundary.

Usage
-----

    from lib.rate_limit.rate_limit_api import check_request, record_tokens

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
    reason: str = ''       # 'tpd' | 'store' | ''
    retry_after_s: float = 0.0
    rpm_limit: int = 0
    rpm_remaining: int = 0
    tpd_limit: int = 0
    tpd_remaining: int = 0


# Credential spending state is separate from request throughput. Personal
# deployment keeps token debt locally; distributed deployment uses the
# selected shared spending authority and its explicit outage policy.
_lock = threading.Lock()
_state: dict[str, dict] = {}

def _state_for(key_id: str, tpd_limit: int) -> dict:
    """Return / lazily create the bucket state for this key."""
    now = time.time()
    entry = _state.get(key_id)
    if entry is None:
        entry = {
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
    tpd = entry['tpd']
    if tpd.capacity != tpd_limit:
        tpd.capacity = float(tpd_limit) if tpd_limit > 0 else 0.0
        tpd.refill_rate = (tpd_limit / 86400.0) if tpd_limit > 0 else 0.0
        tpd.tokens = min(tpd.tokens, tpd.capacity)
    return entry


def _shared_bucket_store():
    """The cross-replica token-bucket store, or None for the in-process path.

    Selection lives in ``lib.rate_limit.rate_limit_store.get_token_bucket_store`` — the
    same deployment-mode contract as ``lib.runtime.runtime_state_store.get_store``.
    """
    from lib.rate_limit.rate_limit_store import get_token_bucket_store
    return get_token_bucket_store()


def _bucket_retry_after(tokens: float, n: float, refill_rate: float) -> float:
    """Same formula as ``_Bucket.retry_after`` for the shared-store path."""
    if tokens >= n:
        return 0.0
    if refill_rate <= 0:
        return float('inf')
    return (n - tokens) / refill_rate


def _backend_failure_decision(store, tpd: int) -> RateDecision:
    """Preserve the spending authority's configured outage policy."""
    if getattr(store, 'fail_open', True):
        logger.warning('[RateLimit] spending store unavailable; personal fail-open policy')
        return RateDecision(allowed=True, tpd_limit=tpd, tpd_remaining=tpd)
    logger.warning('[RateLimit] spending store unavailable; distributed fail-closed policy')
    return RateDecision(allowed=False, reason='store', retry_after_s=5.0,
                        tpd_limit=tpd, tpd_remaining=0)


def _check_request_shared(store, key_id: str, tpd: int, now: float) -> RateDecision:
    """Inspect the explicit spending allowance without consuming request slots."""
    peek = store.bucket_try_consume(
        f'{key_id}:tpd', float(tpd), tpd / 86400.0, 0.0, now)
    if peek is None:
        return _backend_failure_decision(store, tpd)
    tokens = peek[1]
    if tokens <= 0:
        return RateDecision(allowed=False, reason='tpd',
                            retry_after_s=_bucket_retry_after(tokens, 1, tpd / 86400.0),
                            tpd_limit=tpd, tpd_remaining=0)
    return RateDecision(allowed=True, tpd_limit=tpd,
                        tpd_remaining=int(max(0, tokens)))


def check_request(auth_ctx, *, request_cost: int = 1) -> RateDecision:
    """Inspect only the credential's explicit spending allowance.

    The authentication middleware validates the credential first. Request
    count/cost and historical RPM metadata never restrict admitted work.
    Actual token spending is recorded separately after the upstream call.
    """
    if auth_ctx is not None and getattr(auth_ctx, 'via_open_mode', False):
        return check_open_mode_request(request_cost=request_cost)
    if auth_ctx is None or not auth_ctx.key_id:
        return RateDecision(allowed=True)
    tpd = max(0, int(auth_ctx.rate_limit_tpd or 0))
    if tpd == 0:
        return RateDecision(allowed=True)
    store = _shared_bucket_store()
    if store is not None:
        return _check_request_shared(store, auth_ctx.key_id, tpd, time.time())
    now = time.time()
    with _lock:
        entry = _state_for(auth_ctx.key_id, tpd)
        bucket: _Bucket = entry['tpd']
        bucket.refill(now)
        if bucket.tokens <= 0:
            return RateDecision(allowed=False, reason='tpd',
                                retry_after_s=bucket.retry_after(1, now),
                                tpd_limit=tpd, tpd_remaining=0)
        return RateDecision(allowed=True, tpd_limit=tpd,
                            tpd_remaining=int(max(0, bucket.tokens)))


def check_open_mode_request(*, request_cost: int = 1,
                            client_ip: str | None = None) -> RateDecision:
    """Project an admitted open-mode request without a resource quota.

    Peer admission belongs to the authentication boundary. Legacy cost/IP
    arguments remain accepted for callers; they allocate no counters, workers,
    or backend state. Retired RPM/exemption environment settings have no effect.
    """
    return RateDecision(allowed=True)


def record_tokens(key_id: str, n_tokens: int, *, rpm_limit: int = 0,
                   tpd_limit: int = 0) -> None:
    """After an LLM call completes, deduct its actual usage from the TPD bucket.

    Called from chat / agent routes once they know the upstream usage.
    Negative bucket values are allowed (we already paid for the call) —
    they recover at the bucket's refill rate.
    """
    if not key_id or n_tokens <= 0:
        return
    store = _shared_bucket_store()
    if store is not None:
        store.bucket_consume_force(
            f'{key_id}:tpd', float(n_tokens), time.time(),
            capacity=float(tpd_limit) if tpd_limit > 0 else None,
            refill_rate=(tpd_limit / 86400.0) if tpd_limit > 0 else None)
        return
    now = time.time()
    with _lock:
        entry = _state.get(key_id)
        if entry is None:
            if tpd_limit <= 0:
                return
            entry = _state_for(key_id, tpd_limit)
        entry['tpd'].consume_force(n_tokens, now)


def api_rate_limit_stats() -> dict[str, int]:
    """Return the payload-free resident API-key state envelope."""
    with _lock:
        return {
            'entries': len(_state),
        }


def apply_headers(response, decision: RateDecision) -> None:
    """Attach standard rate-limit headers to a Flask/Quart response."""
    if not decision or decision.tpd_limit <= 0:
        return
    try:
        if decision.tpd_limit > 0:
            response.headers['X-RateLimit-Limit-Tokens'] = str(decision.tpd_limit)
            response.headers['X-RateLimit-Remaining-Tokens'] = str(decision.tpd_remaining)
        if not decision.allowed and decision.retry_after_s > 0:
            response.headers['Retry-After'] = str(int(decision.retry_after_s) + 1)
    except Exception as e:
        logger.debug('[RateLimit] header injection failed: %s', e)


__all__ = ['RateDecision', 'check_request', 'check_open_mode_request',
           'record_tokens', 'apply_headers', 'api_rate_limit_stats']

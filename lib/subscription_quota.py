"""Capture subscription quota snapshots exposed by Codex responses.

The ChatGPT-backed Codex endpoint is not a normal metered API account: the
response ``usage`` object reports tokens, while the subscription allowance is
reported separately in ``x-codex-*`` response headers.  Keep those two facts
separate.  This module projects the response headers into a small, durable
``usage['_subscription_quota']`` snapshot and retains the latest snapshot for
the OAuth settings card.

The upstream percentage is account-wide and deliberately coarse.  The
``observed_delta_percent`` field is therefore only the difference between two
adjacent snapshots, not a billing-grade per-request debit.  Consumers must
label it as observed/approximate, especially when several conversations share
the same subscription concurrently.
"""

from __future__ import annotations

import copy
import hashlib
import threading
import time
from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from lib.ttl_cache import TTLCache


USAGE_QUOTA_KEY = '_subscription_quota'
logger = get_logger(__name__)

_MAX_QUOTA_IDENTITIES = 256
_MAX_CACHE_KEY_CHARS = 256
_latest_snapshots = TTLCache(
    ttl=0,
    max_size=_MAX_QUOTA_IDENTITIES,
    name='subscription_quota',
)
_quota_update_lock = threading.Lock()


def _normalize_cache_key(value: object) -> str:
    key = str(value or 'codex').strip() or 'codex'
    if len(key) <= _MAX_CACHE_KEY_CHARS:
        return key
    digest = hashlib.sha256(key.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'


def _headers_lower(headers: Mapping | None) -> dict[str, str]:
    if not headers:
        return {}
    try:
        return {str(key).lower(): str(value) for key, value in headers.items()}
    except (AttributeError, TypeError, ValueError) as exc:
        logger.debug('[SubscriptionQuota] invalid header mapping ignored: %s',
                     exc)
        return {}


def _number(value: str | None) -> float | None:
    if value is None or value == '':
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        logger.debug('[SubscriptionQuota] invalid numeric header %r: %s',
                     value, exc)
        return None
    if parsed != parsed or parsed in (float('inf'), float('-inf')):
        return None
    return parsed


def _integer(value: str | None) -> int | None:
    parsed = _number(value)
    if parsed is None:
        return None
    return int(parsed)


def _window(headers: dict[str, str], name: str) -> dict[str, Any] | None:
    prefix = f'x-codex-{name}-'
    used = _number(headers.get(prefix + 'used-percent'))
    if used is None:
        return None
    used = round(max(0.0, min(used, 100.0)), 3)
    out: dict[str, Any] = {
        'used_percent': used,
        'remaining_percent': round(100.0 - used, 3),
    }
    minutes = _integer(headers.get(prefix + 'window-minutes'))
    if minutes is not None and 0 < minutes <= 10 * 366 * 24 * 60:
        out['window_minutes'] = minutes
    # These names are accepted defensively for compatible relays.  Current
    # Codex clients only require used-percent/window-minutes, so absence of a
    # reset timestamp is normal and must not be replaced by a fabricated one.
    resets_at = (_integer(headers.get(prefix + 'resets-at'))
                 or _integer(headers.get(prefix + 'reset-at')))
    if resets_at is not None and 0 < resets_at <= 253_402_300_799:
        out['resets_at'] = resets_at
    return out


def parse_codex_quota_headers(
        headers: Mapping | None, *, now: float | None = None,
) -> dict[str, Any] | None:
    """Return a normalized Codex quota snapshot, or ``None`` when absent."""
    lower = _headers_lower(headers)
    primary = _window(lower, 'primary')
    secondary = _window(lower, 'secondary')
    if primary is None and secondary is None:
        return None

    snapshot: dict[str, Any] = {
        'provider': 'codex',
        'source': 'response_headers',
        'captured_at': int(time.time() if now is None else now),
        'primary': primary,
        'secondary': secondary,
    }
    plan_type = str(lower.get('x-codex-plan-type') or '').strip()
    if plan_type:
        snapshot['plan_type'] = plan_type[:64]
    return snapshot


def _same_window(previous: dict, current: dict) -> bool:
    prev_minutes = previous.get('window_minutes')
    cur_minutes = current.get('window_minutes')
    return (prev_minutes is None or cur_minutes is None
            or prev_minutes == cur_minutes)


def _add_observed_deltas(snapshot: dict, previous: dict | None) -> None:
    if not isinstance(previous, dict):
        return
    for name in ('primary', 'secondary'):
        current = snapshot.get(name)
        before = previous.get(name)
        if not isinstance(current, dict) or not isinstance(before, dict):
            continue
        if not _same_window(before, current):
            continue
        old_used = _number(before.get('used_percent'))
        new_used = _number(current.get('used_percent'))
        # A lower percentage means the rolling window reset or the upstream
        # changed limit identity.  Do not turn that into a negative debit.
        if old_used is None or new_used is None or new_used < old_used:
            continue
        current['observed_delta_percent'] = round(new_used - old_used, 3)
        current['has_previous_snapshot'] = True


def record_codex_quota(
        headers: Mapping | None, usage: dict | None = None, *,
        now: float | None = None, cache_key: str = 'codex',
) -> dict | None:
    """Attach a response quota snapshot to ``usage`` and update the cache.

    Returns the supplied usage mapping (or a new mapping when ``usage`` was
    ``None``).  With no Codex quota headers, the original value is returned
    unchanged.
    """
    snapshot = parse_codex_quota_headers(headers, now=now)
    if snapshot is None:
        return usage
    cache_key = _normalize_cache_key(cache_key)
    with _quota_update_lock:
        previous = _latest_snapshots.get(cache_key)
        _add_observed_deltas(snapshot, previous)
        _latest_snapshots.set(cache_key, copy.deepcopy(snapshot))
    target = usage if isinstance(usage, dict) else {}
    target[USAGE_QUOTA_KEY] = snapshot
    return target


def latest_subscription_quota(provider: str = 'codex', *,
                              now: float | None = None,
                              cache_key: str | None = None) -> dict | None:
    """Return a copy of the process's latest successful quota snapshot."""
    lookup_key = _normalize_cache_key(cache_key or provider)
    cached = _latest_snapshots.get(lookup_key)
    snapshot = copy.deepcopy(cached) if cached else None
    if snapshot is None:
        return None
    captured_at = int(snapshot.get('captured_at') or 0)
    current = int(time.time() if now is None else now)
    snapshot['age_seconds'] = max(0, current - captured_at)
    return snapshot


def clear_subscription_quota(provider: str = 'codex', *,
                             cache_key: str | None = None) -> None:
    """Forget a cached snapshot when its subscription identity logs out."""
    _latest_snapshots.invalidate(_normalize_cache_key(cache_key or provider))


def _reset_subscription_quota_cache_for_tests() -> None:
    _latest_snapshots.clear()


__all__ = [
    'USAGE_QUOTA_KEY',
    'parse_codex_quota_headers',
    'record_codex_quota',
    'latest_subscription_quota',
    'clear_subscription_quota',
]

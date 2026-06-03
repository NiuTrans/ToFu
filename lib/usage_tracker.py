"""lib/usage_tracker.py — Per-API-key usage analytics.

Records two counters per (key_id, day):
  * ``requests``   — number of API calls
  * ``tokens``     — total LLM tokens consumed (prompt + completion)

Backs ``GET /api/v1/usage`` and the Prometheus ``/metrics`` exposition.

Storage: ``data/config/usage.json`` via ``lib.json_store``. Single
process, in-memory + atomic flush. We deliberately keep this off the
PostgreSQL/SQLite database — usage data is fire-and-forget,
non-critical, and a JSON file scales fine for the per-key fan-out we
expect (dozens of keys × 90 days = sub-100 KB).

  {
    "version": 1,
    "days":  {
        "2026-05-25": {
            "k_a3f2c1": {"requests": 42, "tokens": 19182,
                          "by_model": {"claude-opus-4-7": 19182}}
        },
        "2026-05-26": { ... }
    }
  }

Retention: 90 days, pruned on every flush.
"""

from __future__ import annotations

import datetime
import threading
import time

from lib.config_dir import config_path
from lib.json_store import read_json, update_json_atomic
from lib.log import get_logger

logger = get_logger(__name__)

_STORE_PATH = config_path('usage.json')
_RETENTION_DAYS = 90

# Flush at most once every N seconds — keeps disk traffic low while
# preserving sub-minute freshness. Hot path runs purely in-memory.
_FLUSH_INTERVAL_S = 30

_lock = threading.RLock()
_state: dict[str, dict] = {}  # day → key_id → counters
_loaded = False
_dirty = False
_last_flush = 0.0


def _utcnow() -> datetime.datetime:
    """Naive UTC ``now`` — keeps the on-disk format date-only and stable
    across Python versions. ``datetime.utcnow()`` is deprecated in 3.12."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _today() -> str:
    return _utcnow().strftime('%Y-%m-%d')


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    with _lock:
        if _loaded:
            return
        data = read_json(_STORE_PATH, default={'version': 1, 'days': {}})
        if isinstance(data, dict) and isinstance(data.get('days'), dict):
            _state.update(data['days'])
        _loaded = True
        logger.debug('[Usage] loaded %d day-buckets from %s',
                     len(_state), _STORE_PATH)


def _prune() -> None:
    """Drop buckets older than ``_RETENTION_DAYS`` days."""
    cutoff = (_utcnow()
              - datetime.timedelta(days=_RETENTION_DAYS)
              ).strftime('%Y-%m-%d')
    drop = [d for d in list(_state) if d < cutoff]
    for d in drop:
        _state.pop(d, None)
    if drop:
        logger.info('[Usage] pruned %d day(s) older than %s',
                    len(drop), cutoff)


def _flush_locked() -> None:
    """Persist current state. Caller must hold ``_lock``."""
    global _dirty, _last_flush
    if not _dirty:
        return
    _prune()
    snapshot = {'version': 1, 'days': dict(_state)}
    try:
        update_json_atomic(_STORE_PATH, lambda _: snapshot, default=snapshot)
        _dirty = False
        _last_flush = time.time()
    except Exception as e:
        logger.warning('[Usage] flush failed: %s', e)


def _maybe_flush_locked() -> None:
    if not _dirty:
        return
    if time.time() - _last_flush >= _FLUSH_INTERVAL_S:
        _flush_locked()


def record(key_id: str, *, n_tokens: int = 0,
           model: str = '', request_count: int = 1) -> None:
    """Record one API call. Cheap (in-memory) — disk flush is amortised.

    ``key_id`` may be empty for tunnel-token / unauthenticated callers;
    those go into a synthetic ``'_anon'`` bucket so dashboards still
    show traffic.
    """
    if not key_id:
        key_id = '_anon'
    _ensure_loaded()
    day = _today()
    with _lock:
        global _dirty
        bucket = _state.setdefault(day, {})
        row = bucket.setdefault(key_id, {
            'requests': 0, 'tokens': 0, 'by_model': {}
        })
        row['requests'] += int(max(0, request_count))
        if n_tokens > 0:
            row['tokens'] += int(n_tokens)
            if model:
                row['by_model'][model] = row['by_model'].get(model, 0) + int(n_tokens)
        _dirty = True
        _maybe_flush_locked()


def flush() -> None:
    """Force-flush to disk. Called at shutdown or from /metrics."""
    _ensure_loaded()
    with _lock:
        _flush_locked()


def usage_for_key(key_id: str, *, days: int = 30) -> dict:
    """Return per-day counters for ``key_id`` for the last ``days`` days.

    Output shape::

        {
          "key_id": "k_a3f2c1",
          "days": [
            {"date": "2026-05-25", "requests": 42, "tokens": 19182,
             "by_model": {"claude-opus-4-7": 19182}},
            ...
          ],
          "total": {"requests": 1234, "tokens": 567890}
        }
    """
    _ensure_loaded()
    end = _utcnow().date()
    start = end - datetime.timedelta(days=max(1, days) - 1)
    out_days = []
    total_req = 0
    total_tok = 0
    with _lock:
        d = start
        while d <= end:
            ds = d.strftime('%Y-%m-%d')
            bucket = _state.get(ds, {}).get(key_id) or {
                'requests': 0, 'tokens': 0, 'by_model': {}
            }
            out_days.append({
                'date': ds,
                'requests': int(bucket.get('requests') or 0),
                'tokens': int(bucket.get('tokens') or 0),
                'by_model': dict(bucket.get('by_model') or {}),
            })
            total_req += int(bucket.get('requests') or 0)
            total_tok += int(bucket.get('tokens') or 0)
            d += datetime.timedelta(days=1)
    return {
        'key_id': key_id,
        'days': out_days,
        'total': {'requests': total_req, 'tokens': total_tok},
    }


def usage_summary(*, days: int = 7) -> dict:
    """Return totals across ALL keys for the last ``days`` days.

    Used by /metrics and the admin overview.
    """
    _ensure_loaded()
    end = _utcnow().date()
    start = end - datetime.timedelta(days=max(1, days) - 1)
    per_key: dict[str, dict] = {}
    daily: list[dict] = []
    with _lock:
        d = start
        while d <= end:
            ds = d.strftime('%Y-%m-%d')
            bucket = _state.get(ds, {}) or {}
            day_req = 0
            day_tok = 0
            for kid, row in bucket.items():
                row_req = int(row.get('requests') or 0)
                row_tok = int(row.get('tokens') or 0)
                day_req += row_req
                day_tok += row_tok
                kp = per_key.setdefault(kid, {'requests': 0, 'tokens': 0})
                kp['requests'] += row_req
                kp['tokens'] += row_tok
            daily.append({'date': ds, 'requests': day_req, 'tokens': day_tok})
            d += datetime.timedelta(days=1)
    return {
        'window_days': days,
        'daily': daily,
        'per_key': per_key,
    }


def all_keys_with_activity() -> list[str]:
    """Return every key_id that has any recorded activity (for /metrics)."""
    _ensure_loaded()
    out: set[str] = set()
    with _lock:
        for bucket in _state.values():
            out.update(bucket.keys())
    return sorted(out)


__all__ = ['record', 'flush', 'usage_for_key', 'usage_summary',
           'all_keys_with_activity']

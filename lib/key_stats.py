"""lib/key_stats.py — Per-day per-key success/failure tracking with auto-disable.

Tracks daily request outcomes per (provider_id, key_name) pair and automatically
disables a key for the rest of the day when it proves unhealthy
(attempts ≥ MIN_ATTEMPTS and success rate < MIN_SUCCESS_RATE).

Users can also manually toggle a key for the day via the Settings UI
(routes/key_stats_routes). Manual overrides take precedence over auto-disable
and are reset the following day.

Rate-limit errors (HTTP 429) are tracked separately because provider 429
messages are ambiguous — the SAME error body can mean "RPM overrun, retry in
a moment" or "balance exhausted, give up forever". We therefore rely on a
streak heuristic rather than trying to parse the body: a key that returns
429 MAX_CONSECUTIVE_429 times IN A ROW without a single success is marked
exhausted for the day. Any success or non-429 error resets the streak.

Last-resort guard:
  The auto-disable logic (exhausted flag + success-rate threshold) will NEVER
  leave a provider with zero usable keys.  If disabling a key would remove the
  last raw-enabled key from its provider, it is kept enabled as a "last resort"
  (see :func:`is_key_enabled`).  Explicit user overrides ``set_key_override(..,
  False)`` still take precedence and can force-disable even the last key.

Persistence:
  data/config/key_stats.json
  {
    "day": "2026-04-18",
    "stats": {
      "providerId::key_name": {
        "success": 12, "failure": 3,
        "rate_limited": 48, "consecutive_429": 5,
        "last_error": "...", "exhausted": false
      },
      ...
    },
    "overrides": {
      "providerId::key_name": true   # true = enabled, false = disabled
    }
  }

Thread-safe. Reads happen on the dispatcher hot path, so an in-memory snapshot
is kept and only persisted on writes.
"""

import json
import os
import threading
import time
from datetime import date

from lib.config_dir import config_path
from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'record_outcome',
    'record_rate_limit',
    'mark_key_exhausted',
    'get_today_stats',
    'get_all_stats',
    'is_key_enabled',
    'set_key_override',
    'clear_key_override',
    'MIN_ATTEMPTS',
    'MIN_SUCCESS_RATE',
    'MAX_CONSECUTIVE_429',
]

# ── Auto-disable thresholds ──
# A key is auto-disabled for the rest of the day when BOTH:
#   1. total attempts today >= MIN_ATTEMPTS  (avoid flapping on 1-2 failures)
#   2. success rate today < MIN_SUCCESS_RATE
MIN_ATTEMPTS = 5
MIN_SUCCESS_RATE = 0.5

# Consecutive-429 threshold. Provider 429 bodies are cryptic and ambiguous
# (some paid keys say the exact same thing on RPM-overrun as on balance-out).
# A key that returns 429 this many times IN A ROW without a single success is
# effectively dead for the day — either it's out of balance or shared quota
# is saturated; in both cases, stop wasting requests on it.
#
# Any success or non-429 error resets the counter to 0.
MAX_CONSECUTIVE_429 = 100

_STATS_PATH = config_path('key_stats.json')
_lock = threading.Lock()
_cache = {
    'day': '',        # YYYY-MM-DD of currently loaded data
    'stats': {},      # {pair_key: {'success': int, 'failure': int, 'last_error': str}}
    'overrides': {},  # {pair_key: bool}  # explicit user overrides for today
    'loaded': False,
}

# ── Siblings lookup cache ──
# Cached list of pair-keys (provider_id::key_name) per provider_id, re-read
# from server_config.json every _SIBLINGS_TTL_SEC seconds.  Held under a
# dedicated lock so the siblings lookup never contends with the hot-path
# stats lock above (the hot path reads siblings OUTSIDE _lock and only
# passes the already-computed list into the locked block).
_SIBLINGS_TTL_SEC = 30.0
_siblings_lock = threading.Lock()
_siblings_cache = {
    'ts': 0.0,
    'by_provider': {},   # {provider_id: [pair_key, ...]}
}

# Track which (day, pk) combinations have already emitted the "last-resort"
# info log so we don't spam the log on every dispatch call.
_last_resort_logged: set[tuple[str, str]] = set()


def _today() -> str:
    return date.today().isoformat()


def _pair_key(provider_id: str, key_name: str) -> str:
    return f'{provider_id or "default"}::{key_name or ""}'


def _list_siblings(provider_id: str) -> list:
    """Return the list of pair-keys configured under *provider_id*.

    Sourced from ``data/config/server_config.json`` via
    :func:`lib._load_server_config`.  Cached for ``_SIBLINGS_TTL_SEC`` seconds
    to avoid re-parsing the config on every dispatch call.

    The returned names follow the convention produced by
    :meth:`LLMDispatcher._build_slots_from_providers` — i.e. each key in a
    provider's ``api_keys`` list becomes ``<provider_id>_key_<i>``.

    For legacy env-var deployments (no ``providers`` in the config) this
    enumerates ``LLM_API_KEYS`` under the ``'default'`` provider.

    Scope = same *provider_id* only.  Cross-provider "last key" counting is
    deliberately incorrect (a YourProvider key shouldn't be kept alive just because
    the user also has an OpenAI key).
    """
    now = time.monotonic()
    with _siblings_lock:
        if (now - _siblings_cache['ts']) < _SIBLINGS_TTL_SEC:
            cached = _siblings_cache['by_provider'].get(provider_id or 'default')
            if cached is not None:
                return list(cached)

    # Rebuild the cache outside any other lock — config I/O can be slow.
    by_provider: dict = {}
    try:
        from lib import _load_server_config
        cfg = _load_server_config() or {}
        providers = cfg.get('providers') or []
        if providers:
            for p in providers:
                pid = p.get('id') or 'default'
                keys = p.get('api_keys') or []
                pair_keys = [_pair_key(pid, f'{pid}_key_{i}')
                             for i in range(len(keys))]
                if pair_keys:
                    by_provider[pid] = pair_keys
        else:
            # Legacy env-var setup — dispatcher names keys 'key_0', 'key_1', …
            # under provider_id='default' (see dispatcher._build_slots_from_env).
            from lib import LLM_API_KEYS
            pair_keys = [_pair_key('default', f'key_{i}')
                         for i in range(len(LLM_API_KEYS))]
            if pair_keys:
                by_provider['default'] = pair_keys
    except Exception as e:
        logger.debug('[KeyStats] siblings lookup failed (non-fatal): %s', e)
        by_provider = {}

    with _siblings_lock:
        _siblings_cache['ts'] = now
        _siblings_cache['by_provider'] = by_provider

    return list(by_provider.get(provider_id or 'default', []))


def _load_unlocked():
    """Load stats from disk. Caller must hold _lock. Handles day rollover."""
    today = _today()
    if not os.path.isfile(_STATS_PATH):
        _cache['day'] = today
        _cache['stats'] = {}
        _cache['overrides'] = {}
        _cache['loaded'] = True
        return
    try:
        with open(_STATS_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('[KeyStats] Failed to read %s: %s — starting fresh',
                       _STATS_PATH, e)
        data = {}

    stored_day = data.get('day') or ''
    if stored_day != today:
        # Day has rolled over — reset stats and overrides.
        logger.info('[KeyStats] Day rollover %s -> %s — resetting stats/overrides',
                    stored_day or '(none)', today)
        _cache['day'] = today
        _cache['stats'] = {}
        _cache['overrides'] = {}
        # Reset the "logged once per day" set on rollover.
        _last_resort_logged.clear()
    else:
        _cache['day'] = stored_day
        _cache['stats'] = data.get('stats') or {}
        _cache['overrides'] = data.get('overrides') or {}
    _cache['loaded'] = True


def _save_unlocked():
    """Persist cache to disk. Caller must hold _lock."""
    try:
        os.makedirs(os.path.dirname(_STATS_PATH), exist_ok=True)
        tmp = _STATS_PATH + '.tmp'
        payload = {
            'day': _cache['day'],
            'stats': _cache['stats'],
            'overrides': _cache['overrides'],
        }
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _STATS_PATH)
    except OSError as e:
        logger.warning('[KeyStats] Failed to persist %s: %s', _STATS_PATH, e)


def _ensure_fresh_unlocked():
    """Make sure cache is loaded and reset if the calendar day has changed."""
    if not _cache['loaded']:
        _load_unlocked()
        return
    today = _today()
    if _cache['day'] != today:
        logger.info('[KeyStats] Day rollover (in-memory) %s -> %s',
                    _cache['day'], today)
        _cache['day'] = today
        _cache['stats'] = {}
        _cache['overrides'] = {}
        _last_resort_logged.clear()
        _save_unlocked()


# ══════════════════════════════════════════════════════════════
#  Public API — hot path
# ══════════════════════════════════════════════════════════════

def _new_entry() -> dict:
    return {
        'success': 0,
        'failure': 0,
        'rate_limited': 0,       # count of 429s today (informational)
        'consecutive_429': 0,    # current streak of 429s with no success
        'last_error': '',
        'exhausted': False,
    }


def record_outcome(provider_id: str, key_name: str, success: bool,
                   error: str = '') -> None:
    """Record a single request outcome (non-429).

    Called from Slot.record_success / Slot.record_error. Must be cheap:
    updates the in-memory counter and persists asynchronously-safe.

    Rate-limit (HTTP 429) errors should call :func:`record_rate_limit`
    instead — they're tracked separately because they reflect contention
    or balance exhaustion, not request-level key health.
    """
    if not key_name:
        return
    pk = _pair_key(provider_id, key_name)
    with _lock:
        _ensure_fresh_unlocked()
        entry = _cache['stats'].get(pk)
        if entry is None:
            entry = _new_entry()
            _cache['stats'][pk] = entry
        # Any non-429 outcome (success OR hard failure) breaks a 429 streak —
        # the key is clearly capable of returning something else.
        entry['consecutive_429'] = 0
        if success:
            entry['success'] = int(entry.get('success') or 0) + 1
        else:
            entry['failure'] = int(entry.get('failure') or 0) + 1
            if error:
                entry['last_error'] = str(error)[:200]
        _save_unlocked()


def record_rate_limit(provider_id: str, key_name: str,
                      reason: str = '') -> bool:
    """Record a 429 for *key_name* and return True if it just got auto-exhausted.

    Tracks a sliding "consecutive 429" counter. Any success or non-429
    error resets it to zero. If the counter reaches MAX_CONSECUTIVE_429,
    the key is flagged as exhausted for the rest of today — no more
    retries, no more wasted requests.

    Unlike :func:`record_outcome`, 429s are NOT counted as failures in
    the success-rate calculation — they're displayed separately.

    Note:
        We still set ``exhausted=True`` on the stats entry even if this key
        is the last raw-enabled one in its provider.  The "last-resort" guard
        lives at READ time in :func:`is_key_enabled` — writing the flag here
        is important for UI surfaces (the "auto-stopped" badge), streak
        tracking, and the manual-override clearing logic.
    """
    if not key_name:
        return False
    pk = _pair_key(provider_id, key_name)
    just_exhausted = False
    with _lock:
        _ensure_fresh_unlocked()
        entry = _cache['stats'].get(pk)
        if entry is None:
            entry = _new_entry()
            _cache['stats'][pk] = entry
        entry['rate_limited'] = int(entry.get('rate_limited') or 0) + 1
        entry['consecutive_429'] = int(entry.get('consecutive_429') or 0) + 1
        if (entry['consecutive_429'] >= MAX_CONSECUTIVE_429
                and not entry.get('exhausted')):
            entry['exhausted'] = True
            just_exhausted = True
            # Only stamp last_error when we actually trip — otherwise the
            # ambiguous 429 body would hide the last real failure.
            if reason:
                entry['last_error'] = str(reason)[:200]
        _save_unlocked()
    if just_exhausted:
        logger.warning(
            '[KeyStats] Key %s hit %d consecutive 429s — marking as '
            'exhausted for today. Last body: %.200s',
            pk, MAX_CONSECUTIVE_429, reason or '')
    return just_exhausted


def mark_key_exhausted(provider_id: str, key_name: str, reason: str = '') -> None:
    """Mark a key as permanently exhausted for the rest of today.

    Called on HTTP 402 / 429-with-insufficient-quota (billing/balance errors).
    Unlike a transient rate-limit, these indicate the key needs a financial
    top-up, so retrying before tomorrow is futile.

    The user can still manually re-enable the key via the Settings UI
    (set_key_override) — e.g. after adding credit — and the exhaustion flag
    is reset at day rollover.

    Note:
        We still set ``exhausted=True`` even if this key is the last
        raw-enabled one in its provider.  The "last-resort" guard lives at
        READ time in :func:`is_key_enabled`, so stats surfaces still reflect
        the billing error while the dispatcher keeps retrying the only key
        available (better than "no slot available" mystery errors).
    """
    if not key_name:
        return
    pk = _pair_key(provider_id, key_name)
    with _lock:
        _ensure_fresh_unlocked()
        entry = _cache['stats'].get(pk)
        if entry is None:
            entry = _new_entry()
            _cache['stats'][pk] = entry
        # Count this as a failure too so the success-rate column reflects it.
        entry['failure'] = int(entry.get('failure') or 0) + 1
        entry['exhausted'] = True
        if reason:
            entry['last_error'] = str(reason)[:200]
        _save_unlocked()
    logger.warning('[KeyStats] Key %s marked as exhausted for today: %s',
                   pk, (reason or '')[:200])


def _raw_enabled_unlocked(pk: str) -> bool:
    """Raw (pre-last-resort) enabled check for one pair-key.

    Caller MUST hold _lock.  Implements the ORIGINAL auto-disable logic:
      1. Manual override wins (True or False).
      2. Exhausted flag (HTTP 402 / insufficient_quota 429) — disable.
      3. Auto-disable (attempts >= MIN_ATTEMPTS AND success rate < threshold).
      4. Otherwise enabled.

    This helper deliberately does NOT know about siblings — the last-resort
    guard is layered on top in :func:`is_key_enabled`.
    """
    if pk in _cache['overrides']:
        return bool(_cache['overrides'][pk])
    entry = _cache['stats'].get(pk) or {}
    if entry.get('exhausted'):
        return False
    s = int(entry.get('success') or 0)
    f = int(entry.get('failure') or 0)
    total = s + f
    if total < MIN_ATTEMPTS:
        return True
    sr = s / total if total else 1.0
    return sr >= MIN_SUCCESS_RATE


def _has_explicit_false_override_unlocked(pk: str) -> bool:
    """Return True iff the user explicitly disabled this key today."""
    ov = _cache['overrides'].get(pk)
    return ov is False


def _is_last_resort_unlocked(pk: str, siblings: list) -> bool:
    """True iff *pk* would be auto-disabled but is its provider's last key.

    Caller MUST hold _lock.  *siblings* is the full list of pair-keys under
    the same provider_id (see :func:`_list_siblings`).  The key is "last
    resort" when:
      - raw check is False, AND
      - the user has NOT explicitly overridden to False, AND
      - every OTHER sibling is also raw-disabled (or there are no others).

    A provider with a single configured key falls under this rule too —
    its sole key never auto-disables.
    """
    if _raw_enabled_unlocked(pk):
        return False
    if _has_explicit_false_override_unlocked(pk):
        return False
    # Consider only siblings that are actually configured in the current
    # provider list.  If *pk* itself is missing from siblings (e.g. stats
    # refer to a removed key), treat it conservatively as NOT last-resort.
    if pk not in siblings:
        return False
    for sib in siblings:
        if sib == pk:
            continue
        if _raw_enabled_unlocked(sib):
            return False
    return True


def is_key_enabled(provider_id: str, key_name: str) -> bool:
    """Return True if this key should be used for new dispatches today.

    Precedence (in order):
      1. Raw check — manual override > exhausted flag > success-rate ≥
         threshold.  If raw check is True, return True.
      2. Explicit user override ``False`` always wins, even if this would
         leave the provider with zero usable keys (users retain full
         control).
      3. Otherwise, the "last-resort" guard: if every sibling key under
         the same ``provider_id`` is also raw-disabled, keep this key
         enabled and log at INFO level (once per day per pair).
      4. Otherwise return False (normal auto-disable).
    """
    if not key_name:
        return True
    pk = _pair_key(provider_id, key_name)

    # Read siblings OUTSIDE the hot-path lock — config I/O is slow and must
    # not block other dispatchers.
    siblings = _list_siblings(provider_id)

    with _lock:
        _ensure_fresh_unlocked()
        if _raw_enabled_unlocked(pk):
            return True
        # Respect explicit manual-disable even when it would zero out the
        # provider — users retain full control.
        if _has_explicit_false_override_unlocked(pk):
            return False
        if _is_last_resort_unlocked(pk, siblings):
            day = _cache['day']
            key = (day, pk)
            if key not in _last_resort_logged:
                _last_resort_logged.add(key)
                # Release _lock before logging?  No — logger is thread-safe
                # and cheap; keeping the check inside the lock avoids races
                # in the "log-once" guard.
                logger.info(
                    '[KeyStats] Keeping %s enabled as last-resort '
                    '(all siblings disabled)', pk)
            return True
        return False


def get_today_stats(provider_id: str, key_name: str) -> dict:
    """Return today's stats for a single key.

    Returns:
        {
          'success': int, 'failure': int, 'total': int,
          'success_rate': float (0..1) | None if total == 0,
          'auto_disabled': bool,           # would auto-disable if no override
          'exhausted': bool,
          'last_resort': bool,             # kept enabled as provider's last key
          'override': bool | None,         # explicit user override, None if none
          'enabled': bool,                 # final effective state
          'last_error': str,
        }
    """
    pk = _pair_key(provider_id, key_name)
    siblings = _list_siblings(provider_id)
    with _lock:
        _ensure_fresh_unlocked()
        entry = _cache['stats'].get(pk) or {}
        s = int(entry.get('success') or 0)
        f = int(entry.get('failure') or 0)
        rl = int(entry.get('rate_limited') or 0)
        cons429 = int(entry.get('consecutive_429') or 0)
        last_err = str(entry.get('last_error') or '')
        exhausted = bool(entry.get('exhausted'))
        total = s + f
        sr = (s / total) if total else None
        auto_disabled = (total >= MIN_ATTEMPTS
                         and sr is not None
                         and sr < MIN_SUCCESS_RATE)
        override = _cache['overrides'].get(pk)
        last_resort = _is_last_resort_unlocked(pk, siblings)
        if override is None:
            enabled = not (exhausted or auto_disabled) or last_resort
        else:
            enabled = bool(override)
        return {
            'success': s,
            'failure': f,
            'rate_limited': rl,
            'consecutive_429': cons429,
            'total': total,
            'success_rate': sr,
            'auto_disabled': auto_disabled,
            'exhausted': exhausted,
            'last_resort': last_resort,
            'override': override,
            'enabled': enabled,
            'last_error': last_err,
            'day': _cache['day'],
        }


def get_all_stats() -> dict:
    """Return a snapshot of all stats for today.

    Returns:
        {
          'day': 'YYYY-MM-DD',
          'min_attempts': int,
          'min_success_rate': float,
          'keys': {
             'providerId::key_name': {<same fields as get_today_stats>,
                                      'last_resort': bool}
          }
        }
    """
    # Pre-compute siblings for every provider_id we'll touch.  Snapshot this
    # outside the stats lock.
    provider_ids_seen: set = set()
    with _lock:
        _ensure_fresh_unlocked()
        for pk in list(_cache['stats'].keys()) + list(_cache['overrides'].keys()):
            if '::' in pk:
                provider_ids_seen.add(pk.split('::', 1)[0])
    siblings_by_provider = {pid: _list_siblings(pid) for pid in provider_ids_seen}

    with _lock:
        _ensure_fresh_unlocked()
        keys_out = {}
        # include any pk that has stats OR override
        all_pks = set(_cache['stats'].keys()) | set(_cache['overrides'].keys())
        for pk in all_pks:
            entry = _cache['stats'].get(pk) or {}
            s = int(entry.get('success') or 0)
            f = int(entry.get('failure') or 0)
            rl = int(entry.get('rate_limited') or 0)
            cons429 = int(entry.get('consecutive_429') or 0)
            exhausted = bool(entry.get('exhausted'))
            total = s + f
            sr = (s / total) if total else None
            auto_disabled = (total >= MIN_ATTEMPTS
                             and sr is not None
                             and sr < MIN_SUCCESS_RATE)
            override = _cache['overrides'].get(pk)
            prov_id = pk.split('::', 1)[0] if '::' in pk else 'default'
            siblings = siblings_by_provider.get(prov_id, [])
            last_resort = _is_last_resort_unlocked(pk, siblings)
            if override is None:
                enabled = not (exhausted or auto_disabled) or last_resort
            else:
                enabled = bool(override)
            keys_out[pk] = {
                'success': s,
                'failure': f,
                'rate_limited': rl,
                'consecutive_429': cons429,
                'total': total,
                'success_rate': sr,
                'auto_disabled': auto_disabled,
                'exhausted': exhausted,
                'last_resort': last_resort,
                'override': override,
                'enabled': enabled,
                'last_error': str(entry.get('last_error') or ''),
            }
        return {
            'day': _cache['day'],
            'min_attempts': MIN_ATTEMPTS,
            'min_success_rate': MIN_SUCCESS_RATE,
            'max_consecutive_429': MAX_CONSECUTIVE_429,
            'keys': keys_out,
        }


def set_key_override(provider_id: str, key_name: str, enabled: bool) -> dict:
    """Explicit user override for today. Returns the updated stats row.

    When a user explicitly re-enables a key (enabled=True), we also clear
    the exhausted flag and reset consecutive_429 — otherwise the counter
    would be full from the previous streak and the very next 429 would
    re-trip the auto-exhaust instantly.
    """
    pk = _pair_key(provider_id, key_name)
    with _lock:
        _ensure_fresh_unlocked()
        _cache['overrides'][pk] = bool(enabled)
        if enabled:
            entry = _cache['stats'].get(pk)
            if entry is not None:
                entry['exhausted'] = False
                entry['consecutive_429'] = 0
        _save_unlocked()
    logger.info('[KeyStats] User override %s=%s (day=%s)',
                pk, bool(enabled), _cache['day'])
    return get_today_stats(provider_id, key_name)


def clear_key_override(provider_id: str, key_name: str) -> dict:
    """Remove explicit override (return to auto-disable logic)."""
    pk = _pair_key(provider_id, key_name)
    with _lock:
        _ensure_fresh_unlocked()
        if pk in _cache['overrides']:
            _cache['overrides'].pop(pk, None)
            _save_unlocked()
            logger.info('[KeyStats] Cleared override for %s (day=%s)',
                        pk, _cache['day'])
    return get_today_stats(provider_id, key_name)


# Eagerly load on import so first dispatch call is fast.
try:
    with _lock:
        _load_unlocked()
except Exception as _e:
    logger.warning('[KeyStats] Eager load failed (non-fatal): %s', _e, exc_info=True)

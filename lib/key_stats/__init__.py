"""lib/key_stats — Per-day per-key success/failure tracking with auto-disable.

Tracks daily request outcomes per (provider_id, key_name) pair and automatically
disables a key for the rest of the day when it proves unhealthy
(attempts ≥ MIN_ATTEMPTS and success rate < MIN_SUCCESS_RATE).

Users can also manually toggle a key via the Settings UI
(routes/key_stats_routes). Manual overrides take precedence over auto-disable
and PERSIST across day rollovers and process restarts — they are only
cleared when the user explicitly removes the override (toggles back to
"auto") via :func:`clear_key_override`. The automatic daily reset applies
to stats (success/failure/429 counters, the ``exhausted`` flag) but NOT to
manual overrides, so a key the user disabled yesterday stays disabled
today.

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

  ``overrides`` is **not** scoped to the stored ``day`` — it carries over
  across day rollovers so manual decisions survive restarts.

Thread-safe. Reads happen on the dispatcher hot path, so an in-memory snapshot
is kept and only persisted on writes.

────────────────────────────────────────────────────────────────────────────
This module was split into a FACADE-PRESERVING PACKAGE.  All the shared
mutable singletons (``_cache``, ``_lock``, ``_siblings_cache``,
``_siblings_lock``, ``_last_resort_logged``, ``_STATS_PATH``,
``_SIBLINGS_TTL_SEC``) live in exactly ONE place — ``lib.key_stats._state`` —
and are re-exported here BY REFERENCE, so every ``from lib.key_stats import X``
call site keeps working byte-identically and there is exactly one ``_cache`` /
``_lock`` in the process.

Sub-modules:
  _state   — shared singletons + low-level cache helpers
  _record  — hot-path outcome recording (record_outcome / record_rate_limit /
             mark_key_exhausted)
  _enable  — raw-enabled + last-resort guard + is_key_enabled
  _query   — read/snapshot API + manual overrides
"""

from lib.log import get_logger

# ── Shared singletons + low-level helpers (BY REFERENCE — never duplicate) ──
from lib.key_stats._state import (
    MAX_CONSECUTIVE_429,
    MIN_ATTEMPTS,
    MIN_SUCCESS_RATE,
    _SIBLINGS_TTL_SEC,
    _STATS_PATH,
    _cache,
    _ensure_fresh_unlocked,
    _last_resort_logged,
    _list_siblings,
    _load_unlocked,
    _lock,
    _new_entry,
    _pair_key,
    _save_unlocked,
    _siblings_cache,
    _siblings_lock,
    _today,
)

# ── Hot-path recording ──
from lib.key_stats._record import (
    mark_key_exhausted,
    record_outcome,
    record_rate_limit,
)

# ── Enable / last-resort decision logic ──
from lib.key_stats._enable import (
    _has_explicit_false_override_unlocked,
    _is_last_resort_unlocked,
    _pick_last_resort_unlocked,
    _rank_for_last_resort_unlocked,
    _raw_enabled_unlocked,
    is_key_enabled,
)

# ── Read/snapshot API + manual overrides ──
from lib.key_stats._query import (
    clear_key_override,
    get_all_stats,
    get_today_stats,
    set_key_override,
)

logger = get_logger(__name__)

# NOTE: preserved verbatim from the original lib/key_stats.py.
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


# Eagerly load on import so first dispatch call is fast.
try:
    with _lock:
        _load_unlocked()
except Exception as _e:
    logger.warning('[KeyStats] Eager load failed (non-fatal): %s', _e, exc_info=True)

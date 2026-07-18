"""lib/key_stats/_state.py — Shared singletons + low-level cache helpers.

This module owns the ONE-AND-ONLY copies of the mutable module-level state
for the whole ``lib.key_stats`` package:

    _cache, _lock, _siblings_cache, _siblings_lock, _last_resort_logged,
    _STATS_PATH, _SIBLINGS_TTL_SEC

Every other submodule imports these BY REFERENCE from here (and the package
``__init__`` re-exports them), so there is exactly one ``_cache`` / ``_lock``
per process.  Never duplicate this state anywhere else.

The low-level helpers that read/mutate the cache under ``_lock`` also live
here: ``_today``, ``_pair_key``, ``_list_siblings``, ``_load_unlocked``,
``_save_unlocked``, ``_ensure_fresh_unlocked``, ``_new_entry``.
"""

import json
import os
import sys as _sys
import threading
import time
from datetime import date

from lib.config_dir import config_path
from lib.log import get_logger

logger = get_logger(__name__)


def _pkg():
    """Return the (possibly partially initialised) ``lib.key_stats`` module.

    Late-bound so internal calls to ``_today`` / ``_list_siblings`` and reads
    of ``_STATS_PATH`` resolve through ``lib.key_stats`` at call time.  This
    preserves the historical monkeypatch contract: test/debug code that does
    ``import lib.key_stats as ks; ks._today = ...`` (or
    ``ks._list_siblings = ...`` / ``ks._STATS_PATH = ...``) still steers the
    behaviour of the low-level helpers below.
    """
    return _sys.modules.get('lib.key_stats')


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
    'overrides': {},  # {pair_key: bool}  # explicit user overrides (PERSISTENT
                      # across day rollovers and restarts)
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


def _stats_path() -> str:
    """Resolve the on-disk stats path, honouring a monkeypatched
    ``lib.key_stats._STATS_PATH`` (the historical test contract).

    Falls back to this module's own ``_STATS_PATH`` when the package attr is
    unavailable (e.g. during package initialisation).
    """
    _pk = _pkg()
    if _pk is not None:
        return getattr(_pk, '_STATS_PATH', _STATS_PATH)
    return _STATS_PATH


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
    deliberately incorrect (a Meituan key shouldn't be kept alive just because
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
    _pk = _pkg()
    today = (_pk._today() if _pk is not None else _today())
    stats_path = _stats_path()
    if not os.path.isfile(stats_path):
        _cache['day'] = today
        _cache['stats'] = {}
        _cache['overrides'] = {}
        _cache['loaded'] = True
        return
    try:
        with open(stats_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('[KeyStats] Failed to read %s: %s — starting fresh',
                       stats_path, e)
        data = {}

    stored_day = data.get('day') or ''
    # Manual overrides PERSIST across day rollovers (and process restarts).
    # Only the daily stats (counters + exhausted flag) reset.
    persisted_overrides = data.get('overrides') or {}
    if stored_day != today:
        # Day has rolled over — reset stats but KEEP overrides so a key
        # the user manually disabled yesterday stays disabled today.
        logger.info(
            '[KeyStats] Day rollover %s -> %s — resetting stats '
            '(preserving %d manual override(s))',
            stored_day or '(none)', today, len(persisted_overrides))
        _cache['day'] = today
        _cache['stats'] = {}
        _cache['overrides'] = persisted_overrides
        # Reset the "logged once per day" set on rollover.
        _last_resort_logged.clear()
        # Persist immediately so the on-disk `day` field advances even if
        # no stats get written today.
        _save_unlocked()
    else:
        _cache['day'] = stored_day
        _cache['stats'] = data.get('stats') or {}
        _cache['overrides'] = persisted_overrides
    _cache['loaded'] = True


def _save_unlocked():
    """Persist cache to disk. Caller must hold _lock.

    Uses ``json_store.write_json_atomic`` which writes to a UNIQUE
    ``mkstemp`` temp file before ``os.replace``.  A fixed ``<path>.tmp``
    name (the previous implementation) raced across processes sharing the
    config dir: two concurrent ``_save_unlocked`` calls wrote the same
    ``key_stats.json.tmp``, and the first ``os.replace`` consumed it so the
    second failed with ``No such file or directory: …key_stats.json.tmp``.
    """
    payload = {
        'day': _cache['day'],
        'stats': _cache['stats'],
        'overrides': _cache['overrides'],
    }
    stats_path = _stats_path()
    try:
        from lib.json_store import write_json_atomic
        write_json_atomic(stats_path, payload)
    except OSError as e:
        logger.warning('[KeyStats] Failed to persist %s: %s', stats_path, e)


def _ensure_fresh_unlocked():
    """Make sure cache is loaded and reset if the calendar day has changed.

    Stats (counters + ``exhausted`` flag) reset at each calendar-day
    boundary, but manual overrides are PERSISTENT — a key the user
    explicitly disabled (or enabled) stays that way until they clear
    the override via the Settings UI.
    """
    if not _cache['loaded']:
        _load_unlocked()
        return
    _pk = _pkg()
    today = (_pk._today() if _pk is not None else _today())
    if _cache['day'] != today:
        logger.info(
            '[KeyStats] Day rollover (in-memory) %s -> %s '
            '(preserving %d manual override(s))',
            _cache['day'], today, len(_cache.get('overrides') or {}))
        _cache['day'] = today
        _cache['stats'] = {}
        # DO NOT touch _cache['overrides'] — manual decisions persist.
        _last_resort_logged.clear()
        _save_unlocked()


def _new_entry() -> dict:
    return {
        'success': 0,
        'failure': 0,
        'rate_limited': 0,       # count of 429s today (informational)
        'consecutive_429': 0,    # current streak of 429s with no success
        'last_error': '',
        'exhausted': False,
    }

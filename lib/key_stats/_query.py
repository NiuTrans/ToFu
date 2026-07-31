"""lib/key_stats/_query.py — Read/snapshot API + manual overrides.

``get_today_stats`` / ``get_all_stats`` / ``set_key_override`` /
``clear_key_override``.  All state lives in ``lib.key_stats._state`` and is
imported here BY REFERENCE; the last-resort helpers come from
``lib.key_stats._enable``.
"""

import sys as _sys

from lib.log import get_logger

from lib.key_stats._state import (
    MIN_ATTEMPTS,
    MIN_SUCCESS_RATE,
    _cache,
    _ensure_fresh_unlocked,
    _list_siblings,
    _lock,
    _pair_key,
    _save_unlocked,
)
from lib.key_stats._enable import _is_last_resort_unlocked

logger = get_logger(__name__)


def _pkg():
    """Return the (possibly partially initialised) ``lib.key_stats`` module.

    Used so the siblings lookup resolves through the package at call time,
    preserving the monkeypatch contract (``ks._list_siblings = ...``).
    """
    return _sys.modules.get('lib.key_stats')


def _siblings_for(provider_id: str) -> list:
    _pk = _pkg()
    fn = (_pk._list_siblings if _pk is not None else _list_siblings)
    return fn(provider_id)


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
    siblings = _siblings_for(provider_id)
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
            'exhausted_models': dict(entry.get('exhausted_models') or {}),
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
    siblings_by_provider = {pid: _siblings_for(pid) for pid in provider_ids_seen}

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
                'exhausted_models': dict(entry.get('exhausted_models') or {}),
            }
        return {
            'day': _cache['day'],
            'min_attempts': MIN_ATTEMPTS,
            'min_success_rate': MIN_SUCCESS_RATE,
            'keys': keys_out,
        }


def set_key_override(provider_id: str, key_name: str, enabled: bool) -> dict:
    """Explicit, PERSISTENT user override. Returns the updated stats row.

    The override is written to ``data/config/key_stats.json`` and survives
    day rollovers and process restarts — a key manually disabled today
    stays disabled until the user explicitly clears the override via
    :func:`clear_key_override`.

    When a user explicitly re-enables a key (enabled=True), we also clear
    the exhausted flag, any PER-MODEL billing-stops, and reset
    consecutive_429 — otherwise the counter would be full from the
    previous streak and the very next 429 would re-trip the auto-exhaust
    instantly. Re-enabling means "I topped up", for every model on the key.
    """
    pk = _pair_key(provider_id, key_name)
    with _lock:
        _ensure_fresh_unlocked()
        _cache['overrides'][pk] = bool(enabled)
        if enabled:
            entry = _cache['stats'].get(pk)
            if entry is not None:
                entry['exhausted'] = False
                entry['exhausted_models'] = {}
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

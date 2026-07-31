"""lib/key_stats/_enable.py — Enable / last-resort decision logic.

Implements the raw auto-disable check plus the "last-resort" guard that
never leaves a provider with zero usable keys.  All state lives in
``lib.key_stats._state`` and is imported here BY REFERENCE.
"""

import sys as _sys

from lib.log import get_logger

from lib.key_stats._state import (
    MIN_ATTEMPTS,
    MIN_SUCCESS_RATE,
    _cache,
    _ensure_fresh_unlocked,
    _last_resort_logged,
    _list_siblings,
    _lock,
    _pair_key,
)

logger = get_logger(__name__)


def _pkg():
    """Return the (possibly partially initialised) ``lib.key_stats`` module.

    Used so ``is_key_enabled`` resolves ``_list_siblings`` / ``_pair_key``
    through the package at call time, preserving the historical monkeypatch
    contract (``import lib.key_stats as ks; ks._list_siblings = ...``).
    """
    return _sys.modules.get('lib.key_stats')


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


def _rank_for_last_resort_unlocked(pk: str) -> tuple:
    """Ranking tuple for last-resort selection. Higher = "healthier".

    Caller MUST hold _lock.  Ordering criteria (highest to lowest weight):
      1. ``not exhausted``  — never-exhausted beats billing/streak-exhausted.
      2. ``success_rate``   — higher is better.
      3. ``success count``  — breaks ties between two 0%-rate keys.
      4. ``-consecutive_429`` — fewer recent 429s is better.
      5. ``-failure count`` — fewer hard failures is better.
    """
    entry = _cache['stats'].get(pk) or {}
    s = int(entry.get('success') or 0)
    f = int(entry.get('failure') or 0)
    cons429 = int(entry.get('consecutive_429') or 0)
    exhausted = bool(entry.get('exhausted'))
    total = s + f
    sr = (s / total) if total else 0.0
    return (not exhausted, sr, s, -cons429, -f)


def _pick_last_resort_unlocked(siblings: list):
    """Pick the single pair-key to keep enabled as last-resort, or None.

    Caller MUST hold _lock.  *siblings* is the list of pair-keys under a
    single ``provider_id`` (see :func:`_list_siblings`).

    Returns:
        - ``None`` if any sibling is raw-enabled (no last-resort needed).
        - ``None`` if the user has explicitly disabled every sibling
          (``override=False``) — respect the user's choice.
        - Otherwise, the pair-key with the "healthiest" stats per
          :func:`_rank_for_last_resort_unlocked`.  Ties broken by later
          index in the configured ``siblings`` list (i.e. the last key wins)
          so behaviour is deterministic and matches the user's intuition
          of "the LAST key is kept".
    """
    # If any sibling is genuinely healthy, nobody needs promotion.
    eligible = []   # list of (idx, pk) tuples for ranking
    for idx, sib in enumerate(siblings):
        if _raw_enabled_unlocked(sib):
            return None
        if _has_explicit_false_override_unlocked(sib):
            continue  # user said no — respect it
        eligible.append((idx, sib))
    if not eligible:
        return None

    # Ranking: healthier wins; higher idx breaks ties ("last key").
    best_idx, best_pk = max(
        eligible,
        key=lambda item: _rank_for_last_resort_unlocked(item[1]) + (item[0],),
    )
    return best_pk


def _is_last_resort_unlocked(pk: str, siblings: list) -> bool:
    """True iff *pk* is THE ONE key chosen to stay alive as last-resort.

    Caller MUST hold _lock.  *siblings* is the full list of pair-keys under
    the same provider_id (see :func:`_list_siblings`).  Returns False for
    the keys that would remain disabled — we deliberately keep only ONE
    alive so the user isn't stuck with (for example) an invalid key soaking
    up requests alongside a merely-rate-limited one.

    A provider with a single configured key falls under this rule too —
    its sole key becomes the "winner" of a 1-element contest and stays
    enabled unless the user has explicitly overridden to False.
    """
    if _raw_enabled_unlocked(pk):
        return False
    if _has_explicit_false_override_unlocked(pk):
        return False
    # pk must actually be configured under its provider; if stats refer to
    # a removed key, don't resurrect it.
    if pk not in siblings:
        return False
    return _pick_last_resort_unlocked(siblings) == pk


def is_key_enabled(provider_id: str, key_name: str, model: str = '') -> bool:
    """Return True if this key should be used for new dispatches today.

    Args:
        model: when given, also honour PER-MODEL billing-stops
            (``exhausted_models`` — see :func:`mark_key_exhausted`). A stop
            recorded for one model does not block sibling models on the same
            key (aggregating-gateway isolation), and a model-specific stop
            never gets last-resort promotion — retrying a quota-dead model
            is futile, the dispatcher should fall back to another model.

    Precedence (in order):
      1. Manual override wins EVERYTHING, including billing-stops — user
         supremacy. (The Settings card surfaces the override-vs-stop
         conflict so a stale manual ON doesn't silently defeat a fresh
         quota error.)
      2. Per-model stop (when *model* given) — disable for that model only.
      3. Raw key-wide check — exhausted flag > success-rate ≥ threshold.
      4. Explicit user override ``False`` always wins, even if this would
         leave the provider with zero usable keys.
      5. Otherwise, the "last-resort" guard: if every sibling key under
         the same ``provider_id`` is raw-disabled, keep exactly ONE of
         them enabled — the "healthiest" per
         :func:`_rank_for_last_resort_unlocked`, with ties broken toward
         the last configured key.  All other siblings stay disabled.
         Logs once per (day, pk) at INFO level when a key is promoted.
      6. Otherwise return False (normal auto-disable).
    """
    if not key_name:
        return True
    _pk = _pkg()
    pk = (_pk._pair_key(provider_id, key_name) if _pk is not None
          else _pair_key(provider_id, key_name))

    # Read siblings OUTSIDE the hot-path lock — config I/O is slow and must
    # not block other dispatchers.  Resolve through the package so tests can
    # monkeypatch ``ks._list_siblings``.
    list_siblings = (_pk._list_siblings if _pk is not None else _list_siblings)
    siblings = list_siblings(provider_id)

    with _lock:
        _ensure_fresh_unlocked()
        # Per-model billing-stop gate. Skip when an override exists — the
        # raw check below already lets the override win, and consulting it
        # twice would double the precedence paths.
        if model and pk not in _cache['overrides']:
            entry = _cache['stats'].get(pk) or {}
            if model in (entry.get('exhausted_models') or {}):
                return False
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

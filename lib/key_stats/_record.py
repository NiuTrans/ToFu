"""lib/key_stats/_record.py — Hot-path outcome recording.

``record_outcome`` / ``record_rate_limit`` / ``mark_key_exhausted`` mutate
the shared in-memory cache under ``_lock`` and persist on write.  All state
lives in ``lib.key_stats._state`` and is imported here BY REFERENCE.
"""

from lib.log import get_logger

from lib.key_stats._state import (
    MAX_CONSECUTIVE_429,
    _cache,
    _ensure_fresh_unlocked,
    _lock,
    _new_entry,
    _pair_key,
    _save_unlocked,
)

logger = get_logger(__name__)


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

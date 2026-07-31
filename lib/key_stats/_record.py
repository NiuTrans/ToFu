"""lib/key_stats/_record.py — Hot-path outcome recording.

``record_outcome`` / ``record_rate_limit`` / ``mark_key_exhausted`` mutate
the shared in-memory cache under ``_lock`` and persist on write.  All state
lives in ``lib.key_stats._state`` and is imported here BY REFERENCE.
"""

from lib.log import get_logger

from lib.key_stats._state import (
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
                      reason: str = '') -> None:
    """Record a 429 for *key_name* — pure telemetry, never a kill signal.

    Tracks the sliding "consecutive 429" counter (any success or non-429
    error resets it to zero) plus the daily ``rate_limited`` total; both are
    shown on the Settings card. That is ALL a 429 may do here.

    Owner policy (2026-07-29, after the sankuai-anthropic total-outage): a
    429 streak NEVER disables a key. 429 is backpressure — the answer is the
    slot-local steering cooldown and RPM decay, so the key rejoins the
    moment the upstream recovers, with zero human action. Only an explicit
    billing-stop (:func:`mark_key_exhausted`, HTTP 402 / quota-exhausted)
    disables a key for the day.

    Unlike :func:`record_outcome`, 429s are NOT counted as failures in the
    success-rate calculation, and the ambiguous 429 body NEVER overwrites
    ``last_error`` (it would hide the last real failure from the UI).
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
        entry['rate_limited'] = int(entry.get('rate_limited') or 0) + 1
        entry['consecutive_429'] = int(entry.get('consecutive_429') or 0) + 1
        _save_unlocked()


def mark_key_exhausted(provider_id: str, key_name: str,
                        reason: str = '', model: str = '') -> None:
    """Mark a key (or one model on it) as exhausted for the rest of today.

    Called on HTTP 402 / 429-with-insufficient-quota (billing/balance errors).
    Unlike a transient rate-limit, these indicate the account needs a
    financial top-up, so retrying before tomorrow is futile.

    Args:
        model: the wire model the quota error was observed on. When given,
            the stop is recorded at **(key, model)** granularity
            (``exhausted_models``) instead of flipping the key-wide
            ``exhausted`` flag. This matters on AGGREGATING GATEWAYS where
            one key proxies several upstream vendors (2026-07-28 incident:
            a qwen→Aliyun ``insufficient_quota`` on ``sankuai_key_1``
            key-wide-exhausted the key, cross-vendor poisoning kimi→Moonshot
            capacity routed through the same key). A single-vendor account
            converges to the same end state — each sibling model trips its
            own billing-stop on its next call — at the cost of one failed
            call per model, which is the honest price of not guessing
            vendor topology from error bodies.

            ``model=''`` is also passed DELIBERATELY for HTTP 402 Payment
            Required (owner ruling 2026-07-29): a 402 is emitted by the
            gateway's OWN credit-validation layer about the ACCOUNT's
            credit pool (sankuai: ext.error.source=AIGC, stage=validation),
            so every model on the key is dead and the key-wide flag is the
            honest stop — per-model there would just burn one live 402
            per remaining model before converging.

    The user can still manually re-enable the key via the Settings UI
    (set_key_override) — e.g. after adding credit — which clears BOTH the
    key-wide flag and all per-model stops; everything resets at day rollover.

    Note:
        Stops are recorded even when this key is the last raw-enabled one in
        its provider.  The "last-resort" guard lives at READ time in
        :func:`is_key_enabled` — and deliberately does NOT promote a
        model-specific billing-stop (retrying a quota-dead model is futile;
        the dispatcher should fall back to another model).
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
        if model:
            entry.setdefault('exhausted_models', {})[model] = \
                str(reason or '')[:200]
        else:
            entry['exhausted'] = True
        if reason:
            entry['last_error'] = str(reason)[:200]
        _save_unlocked()
    logger.warning('[KeyStats] %s exhausted for today (model=%s): %s',
                   pk, model or '<key-wide>', (reason or '')[:200])

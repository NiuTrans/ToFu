"""lib/key_stats/_record.py — Hot-path outcome recording.

``record_outcome`` / ``record_rate_limit`` / ``record_gateway_error`` /
``mark_key_exhausted`` apply semantic increments to the latest durable
document under ``_lock``. All state lives in ``lib.key_stats._state`` and is
imported here BY REFERENCE.
"""

from lib.log import get_logger

from lib.key_stats._state import (
    _lock,
    _new_entry,
    _pair_key,
    _update_store_unlocked,
)

logger = get_logger(__name__)


def _entry_for_update(stats: dict, pk: str) -> dict:
    """Return a mutable entry, repairing a malformed leaf defensively."""
    entry = stats.get(pk)
    if not isinstance(entry, dict):
        entry = _new_entry()
        stats[pk] = entry
    return entry


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

    def _mutate(stats, _overrides):
        entry = _entry_for_update(stats, pk)
        # Any non-429 outcome (success OR hard failure) breaks a 429 streak —
        # the key is clearly capable of returning something else.
        entry['consecutive_429'] = 0
        if success:
            entry['success'] = int(entry.get('success') or 0) + 1
        else:
            entry['failure'] = int(entry.get('failure') or 0) + 1
            if error:
                entry['last_error'] = str(error)[:200]

    with _lock:
        _update_store_unlocked(_mutate)


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

    def _mutate(stats, _overrides):
        entry = _entry_for_update(stats, pk)
        entry['rate_limited'] = int(entry.get('rate_limited') or 0) + 1
        entry['consecutive_429'] = int(entry.get('consecutive_429') or 0) + 1

    with _lock:
        _update_store_unlocked(_mutate)


def record_gateway_error(provider_id: str, key_name: str,
                         reason: str = '') -> None:
    """Record a GATEWAY-class failure for *key_name* — own counter only.

    Gateway-class = HTTP 502/503/504 from the gateway's LB layer,
    upstream-vendor transients wrapped in 4xx, and mid-stream SSE errors.
    These say "the upstream behind the gateway is sick", never "this key is
    sick" — and they hit every key on the provider uniformly, so feeding
    them into the daily success-rate column both wrecks the card's honesty
    and TRIPS the auto-disable gate (2026-08-03 sankuai 502 storm: 813
    failures in ~2h auto-disabled 2 of 3 healthy keys for the day; only the
    third key's success volume plus the last-resort guard kept service up).

    So a gateway error feeds ONLY the ``gateway_errors`` daily counter
    (pure UI telemetry, like ``rate_limited``):
      * NOT ``failure`` — the success-rate denominator and the
        auto-disable gate must never see a gateway storm. The dead-key
        safety net lives on the unchanged classes instead: auth death
        (401/403) and endpoint-unreachable still record genuine failures.
      * NOT ``consecutive_429`` — the 429 streak is per-key contention
        telemetry.
      * NOT ``last_error`` — a bare ``<html>`` 502 body is diagnostic
        garbage that would hide the last real failure from the UI (the
        :func:`record_rate_limit` precedent).
    """
    if not key_name:
        return
    pk = _pair_key(provider_id, key_name)

    def _mutate(stats, _overrides):
        entry = _entry_for_update(stats, pk)
        entry['gateway_errors'] = int(entry.get('gateway_errors') or 0) + 1

    with _lock:
        _update_store_unlocked(_mutate)


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

    def _mutate(stats, _overrides):
        entry = _entry_for_update(stats, pk)
        # Count this as a failure too so the success-rate column reflects it.
        entry['failure'] = int(entry.get('failure') or 0) + 1
        if model:
            exhausted_models = entry.get('exhausted_models')
            if not isinstance(exhausted_models, dict):
                exhausted_models = {}
                entry['exhausted_models'] = exhausted_models
            exhausted_models[model] = str(reason or '')[:200]
        else:
            entry['exhausted'] = True
        if reason:
            entry['last_error'] = str(reason)[:200]

    with _lock:
        _update_store_unlocked(_mutate)
    logger.warning('[KeyStats] %s exhausted for today (model=%s): %s',
                   pk, model or '<key-wide>', (reason or '')[:200])

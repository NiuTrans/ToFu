"""lib/llm_dispatch/conv_affinity.py — Conversation-sticky slot routing.

Why this exists
===============
Anthropic's prompt cache is keyed **per API key**: each upstream key has its
own independent server-side cache namespace.  The dispatcher
(``_pick`` in ``dispatcher.py``) chooses the lowest-``score()`` slot
**independently for every request**, and ``Slot.score()`` adds a
``random.uniform(0.95, 1.05)`` jitter on top of inflight / RPM penalties.  So
when a single model has two or more keys, the chosen ``(key, model)`` slot
flips between rounds of the SAME conversation — and every flip lands on a key
whose cache does not hold this conversation's prefix, costing a full
``cache_creation`` write + 0% read (observed on conv ``mqjlcopple4o60``:
two full ~95–133K-token re-writes inside one 8-round turn, with the older
prefix resurfacing when a request bounced back to the first key).

The fix is conversation affinity: keep a conversation pinned to the key that
last served it, so its prompt-cache prefix keeps getting hit across rounds.
Affinity is a *soft* preference, not a hard pin — if the sticky key is cooled
down (429), excluded, disabled, or otherwise ineligible, the picker silently
falls back to score-based selection and records the new key as sticky.  This
preserves automatic failover and load-spreading onto healthy keys.

Mechanism
---------
Two pieces, mirroring :mod:`lib.llm_dispatch.provider_pin`:

1. A :class:`threading.local` carrying the *current* conversation id.
   ``run_task`` (the orchestrator's per-task worker thread) sets it from
   ``task['convId']`` and clears it on exit (worker threads are pooled and
   reused).  Aux LLM calls on the same thread (compaction summaries, endpoint
   replan turns) inherit it automatically.

2. A process-global ``conv_id → (key_name, ts)`` recency map.  ``_pick``
   records the chosen key here every pick and reads it back next round.  The
   map persists across turns (each turn is a fresh task/thread for the same
   ``convId``), so a follow-up message reuses the prior turn's key while its
   cache is still warm.  Entries older than the TTL are pruned lazily; the map
   is also size-capped to bound memory on a long-lived server.

Disable with ``TOFU_CONV_STICKY_ROUTING=0``.  Tune staleness with
``TOFU_CONV_STICKY_TTL`` (seconds, default 1800).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'sticky_routing_enabled',
    'sticky_hold_enabled',
    'sticky_hold_budget_ms',
    'sticky_hold_max_ms',
    'get_conv_affinity',
    'set_conv_affinity',
    'clear_conv_affinity',
    'conv_affinity',
    'get_preferred_key',
    'record_conv_key',
    'record_pick_decision',
    'get_pick_decision',
]


def sticky_routing_enabled() -> bool:
    """Whether conversation-sticky routing is active (env-gated, default on)."""
    val = os.environ.get('TOFU_CONV_STICKY_ROUTING', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def sticky_hold_enabled() -> bool:
    """Whether to briefly WAIT for a conv's warm key during a short 429 cooldown.

    When the conversation's sticky key is the only thing the prompt-cache prefix
    is warm on, migrating to a cold key on a transient (sub-second) rate-limit
    cooldown costs a full ``cache_creation`` re-write — far more than waiting out
    the per-minute throttle window. With this on (default), the dispatch retry
    loop holds for the warm key up to :func:`sticky_hold_budget_ms` instead of
    immediately rebinding to a cold key. Disable with ``TOFU_CONV_STICKY_HOLD=0``.
    """
    val = os.environ.get('TOFU_CONV_STICKY_HOLD', '1')
    return val.strip().lower() not in ('0', 'false', 'no', 'off', '')


def sticky_hold_budget_ms() -> float:
    """Max time (ms) to wait for a conv's warm key on a short 429 cooldown.

    Caps how long :func:`sticky_hold_enabled` will hold. A remaining cooldown
    longer than this budget is treated as a genuine failure cooldown (the slot
    fell into the consecutive-error backoff / quota-exhaustion path, not the
    0.5s rate-limit nudge) and is NOT waited on — the loop rebinds as before.
    Default 1500ms. Tune with ``TOFU_CONV_STICKY_HOLD_MS``.
    """
    try:
        ms = float(os.environ.get('TOFU_CONV_STICKY_HOLD_MS', '1500'))
        return ms if ms > 0 else 1500.0
    except (ValueError, TypeError) as e:
        logger.debug('[ConvAffinity] TOFU_CONV_STICKY_HOLD_MS parse failed, using default: %s', e)
        return 1500.0


def sticky_hold_max_ms() -> float:
    """Escalated ceiling (ms) to wait for a conv's SOLE warm key under contention.

    ``sticky_hold_budget_ms`` (default 1500) covers a transient sub-second 429
    rate-limit nudge. But on a shared server a *concurrent sibling* can cool the
    conversation's ONLY warm key for longer than that budget — and the old code
    then silently cold-rebound, destroying the prompt-cache prefix on a byte-
    identical round (the mrne3bqe R4 clean-round namespace-flip vector). This
    LARGER bounded ceiling lets the hold wait out that contention window instead
    of instantly landing cold, WHILE still failing over on a genuinely long
    error/quota backoff (e.g. 300s >> ceiling → no hold). It is NOT a hard pin:
    ``sticky_cooldown_remaining_s`` already returns None for an excluded /
    disabled / incompatible key, so a truly-down key never reaches the hold.
    Default 8000ms. Tune with ``TOFU_CONV_STICKY_HOLD_MAX_MS``; set at or below
    the budget to disable escalation (hold reverts to the flat budget window).
    """
    try:
        ms = float(os.environ.get('TOFU_CONV_STICKY_HOLD_MAX_MS', '8000'))
        base = sticky_hold_budget_ms()
        # Never below the base budget — escalation only ever widens the window.
        return ms if ms >= base else base
    except (ValueError, TypeError) as e:
        logger.debug('[ConvAffinity] TOFU_CONV_STICKY_HOLD_MAX_MS parse failed, using default: %s', e)
        return max(8000.0, sticky_hold_budget_ms())


def _ttl_seconds() -> float:
    """Max age (seconds) a conv→key affinity is honored. Default 1800 (30m)."""
    try:
        ttl = float(os.environ.get('TOFU_CONV_STICKY_TTL', '1800'))
        return ttl if ttl > 0 else 1800.0
    except (ValueError, TypeError) as e:
        logger.debug('[ConvAffinity] TOFU_CONV_STICKY_TTL parse failed, using default: %s', e)
        return 1800.0


# Hard cap on the recency map so a long-lived server with many conversations
# can't grow it without bound. When exceeded, the oldest entries are dropped.
_MAX_ENTRIES = 4096


# ── Thread-scoped current conversation ──
_state = threading.local()


def get_conv_affinity() -> str | None:
    """Return the conversation id bound on the current thread, or None."""
    return getattr(_state, 'conv_id', None)


def set_conv_affinity(conv_id: str | None) -> None:
    """Bind the current thread to ``conv_id`` (None / '' clears it).

    Idempotent. Used by ``run_task`` which cannot wrap its long body in a
    ``with`` block; it pairs this with :func:`clear_conv_affinity` in its
    ``finally``.
    """
    _state.conv_id = (conv_id or None)


def clear_conv_affinity() -> None:
    """Remove any conversation binding on the current thread.

    Critical: worker threads are pooled and reused, so a binding left behind
    would bleed into the NEXT unrelated task that lands on this thread.
    """
    _state.conv_id = None


def record_pick_decision(*, preferred_key, chosen_key, fell_back, cooldown_remaining_s=None):
    """Record the LAST sticky-routing pick decision on this thread (diagnostic).

    Read back by the cache byte-probe so a routing-flip capture can say WHY the
    key differed — soft-fallback under cooldown/contention vs affinity never
    engaging. Thread-local so a concurrent sibling's pick can't clobber it, and
    cheap enough to always record (a plain dict assignment). Never affects
    routing behaviour.
    """
    _state.pick_decision = {
        'preferred_key_hash': _hash_key(preferred_key),
        'chosen_key_hash': _hash_key(chosen_key),
        'affinity_fell_back': bool(fell_back),
        'cooldown_remaining_s': cooldown_remaining_s,
    }


def get_pick_decision() -> dict | None:
    """Return the last sticky pick decision recorded on this thread, or None."""
    return getattr(_state, 'pick_decision', None)


def _hash_key(key_name):
    """Salted, truncated hash of a key NAME for the probe (key names are not
    secrets, but hashing keeps the dump uniform with the api-key hash and avoids
    leaking internal key labels into an artifact). Empty/None → ''."""
    if not key_name:
        return ''
    import hashlib
    return hashlib.sha256(('tofu-cache-probe:' + str(key_name)).encode('utf-8')).hexdigest()[:12]


@contextlib.contextmanager
def conv_affinity(conv_id: str | None):
    """Context-manager form — bind for the duration of the block.

    Restores the previous binding on exit (supports nesting). When
    ``conv_id`` is falsy this is a transparent no-op so callers can wrap
    unconditionally.
    """
    if not conv_id:
        yield
        return
    prev = getattr(_state, 'conv_id', None)
    _state.conv_id = conv_id
    try:
        yield
    finally:
        _state.conv_id = prev


# ── Process-global recency map ──
# Scoped entries use ``(conv_id, route_key)``.  A legacy ``conv_id`` string
# entry is retained as a latest-pick compatibility/diagnostic alias; the
# dispatcher never uses that alias when it has a model scope.
# ``route_key`` is normally the requested logical model.  Keeping one key per
# conversation was insufficient: an auxiliary compaction/translation pick on a
# different model overwrote the main model's warm key, causing A→B→A namespace
# churn in the next chat round.
_conv_keys: dict[object, tuple[str, float]] = {}
_conv_lock = threading.Lock()


def _map_key(conv_id: str, route_key: str = ''):
    return (conv_id, route_key) if route_key else conv_id


def _prune_locked(now: float) -> None:
    """Drop stale entries; if still over the cap, drop the oldest. Caller holds lock."""
    ttl = _ttl_seconds()
    stale = [key for key, (_, ts) in _conv_keys.items() if now - ts > ttl]
    for key in stale:
        del _conv_keys[key]
    if len(_conv_keys) > _MAX_ENTRIES:
        # Drop oldest entries down to the cap.
        ordered = sorted(_conv_keys.items(), key=lambda kv: kv[1][1])
        for key, _ in ordered[:len(_conv_keys) - _MAX_ENTRIES]:
            del _conv_keys[key]


def get_preferred_key(conv_id: str, *, route_key: str = '') -> str | None:
    """Return the key_name that last served ``conv_id``, or None if absent/stale."""
    if not conv_id:
        return None
    now = time.time()
    with _conv_lock:
        map_key = _map_key(conv_id, route_key)
        entry = _conv_keys.get(map_key)
        # Rolling-deploy compatibility: an affinity recorded before scoped
        # keys existed is still a valid first preference for any model.  Once
        # the scoped picker succeeds it seeds the exact entry below.
        if entry is None and route_key:
            map_key = conv_id
            entry = _conv_keys.get(map_key)
        if not entry:
            return None
        key_name, ts = entry
        if now - ts > _ttl_seconds():
            del _conv_keys[map_key]
            return None
        return key_name


def record_conv_key(conv_id: str, key_name: str, *, route_key: str = '') -> None:
    """Remember that ``key_name`` served ``conv_id`` (updates recency)."""
    if not conv_id or not key_name:
        return
    now = time.time()
    with _conv_lock:
        _conv_keys[_map_key(conv_id, route_key)] = (key_name, now)
        if route_key:
            # Preserve the historical unscoped API as "latest key for this
            # conversation" without letting it drive scoped dispatcher picks.
            _conv_keys[conv_id] = (key_name, now)
        if len(_conv_keys) > _MAX_ENTRIES:
            _prune_locked(now)

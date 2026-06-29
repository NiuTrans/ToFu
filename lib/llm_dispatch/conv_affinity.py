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
    'get_conv_affinity',
    'set_conv_affinity',
    'clear_conv_affinity',
    'conv_affinity',
    'get_preferred_key',
    'record_conv_key',
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
    except (ValueError, TypeError):
        return 1500.0


def _ttl_seconds() -> float:
    """Max age (seconds) a conv→key affinity is honored. Default 1800 (30m)."""
    try:
        ttl = float(os.environ.get('TOFU_CONV_STICKY_TTL', '1800'))
        return ttl if ttl > 0 else 1800.0
    except (ValueError, TypeError):
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


# ── Process-global recency map: conv_id → (key_name, timestamp) ──
_conv_keys: dict[str, tuple[str, float]] = {}
_conv_lock = threading.Lock()


def _prune_locked(now: float) -> None:
    """Drop stale entries; if still over the cap, drop the oldest. Caller holds lock."""
    ttl = _ttl_seconds()
    stale = [cid for cid, (_, ts) in _conv_keys.items() if now - ts > ttl]
    for cid in stale:
        del _conv_keys[cid]
    if len(_conv_keys) > _MAX_ENTRIES:
        # Drop oldest entries down to the cap.
        ordered = sorted(_conv_keys.items(), key=lambda kv: kv[1][1])
        for cid, _ in ordered[:len(_conv_keys) - _MAX_ENTRIES]:
            del _conv_keys[cid]


def get_preferred_key(conv_id: str) -> str | None:
    """Return the key_name that last served ``conv_id``, or None if absent/stale."""
    if not conv_id:
        return None
    now = time.time()
    with _conv_lock:
        entry = _conv_keys.get(conv_id)
        if not entry:
            return None
        key_name, ts = entry
        if now - ts > _ttl_seconds():
            del _conv_keys[conv_id]
            return None
        return key_name


def record_conv_key(conv_id: str, key_name: str) -> None:
    """Remember that ``key_name`` served ``conv_id`` (updates recency)."""
    if not conv_id or not key_name:
        return
    now = time.time()
    with _conv_lock:
        _conv_keys[conv_id] = (key_name, now)
        if len(_conv_keys) > _MAX_ENTRIES:
            _prune_locked(now)

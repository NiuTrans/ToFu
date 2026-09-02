"""lib.tasks_pkg.cache_tracking._persist — DURABLE per-conversation
cache-prefix boundary.

WHY
---
``get_cache_prefix_count`` protects the cached prefix from micro-compact
mutation, but its state (``_cache_states``) is a PROCESS-MEMORY dict. The
cross-thread fallback (a new user turn on a fresh ``run_task`` thread reusing a
warm sibling-thread entry) closes the common case, but memory does NOT survive:

  1. **Restart** — SIGTERM / crash-reboot between turn A and turn B wipes
     ``_cache_states``; turn B round-1 finds no sibling → boundary collapses to
     0 → micro-compact rewrites the prefix the gateway STILL caches (TTL) → miss.
  2. **Multi-replica** — turn A and turn B land on different server replicas;
     B's process never held A's state → same collapse.

And the messages that get rewritten were PROTECTED last turn (guard active), so
they were never compacted → never persisted a placeholder → still full-text in
storage. An in-memory fallback only says "don't fail THIS time"; it never
freezes them at the storage layer.

This module makes the boundary a DURABLE CONVERSATION FACT: a monotonic
high-water mark of the largest prefix-message-count this conversation has ever
sent as a cached prefix, persisted on the ``conversations.settings`` JSON
(``settings.cachePrefixHWM``). It survives restart AND is cross-replica (shared
DB), with NO schema migration. ``get_cache_prefix_count`` reads it as the
authoritative floor when the in-memory state is cold; ``detect_cache_break``
advances it (monotonically) whenever a warm round confirms a larger cached
prefix.

SAFETY
------
* MONOTONIC — the stored value only ever RISES (``max`` merge under the
  serialized settings lock). A stale/smaller candidate can never lower it.
* Raising the compaction floor is cache-SAFE by construction: it only PROTECTS
  more messages from mutation, never fewer, so restoring a (possibly slightly
  stale) larger boundary can never itself cause a miss — worst case it defers a
  little compaction. Messages are append-only within a conv, so any prior
  boundary is a valid prefix of the current turn.
* Best-effort: every DB touch is guarded; a failure degrades to the in-memory
  path, never breaks a round.

READ CACHE
----------
The cold-thread fallback runs once per compaction pass (once per round). A tiny
in-process TTL cache over the DB read keeps that from hitting the DB every
round while still picking up a cross-replica advance within ``_HWM_TTL_S``.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

# settings key for the durable high-water mark.
_HWM_KEY = 'cachePrefixHWM'

# settings key for the durable per-conv round-1 cache_read baseline. Unlike the
# HWM (a monotonic message-count floor), this is the PREVIOUS turn's FINAL
# cached-prefix read in TOKENS — last-writer-wins (NOT max), so it tracks the
# real prior warm read and never leaves a stale-high value that would make
# every later floor-only read look like a collapse forever.
_LAST_TURN_READ_KEY = 'lastTurnCacheRead'

# Read-cache TTL: the cold-thread fallback reads at most once per this window.
_HWM_TTL_S = 30.0

# (user_id, conv_id) → (value, expires_at). Guarded by _hwm_lock.
_hwm_read_cache: dict[tuple[int, str], tuple[int, float]] = {}
# Owner-scoped round-1 read baseline. Guarded by _hwm_lock.
_last_turn_read_cache: dict[tuple[int, str], tuple[int, float]] = {}
_hwm_lock = threading.Lock()

# Per-conv throttle for write-failure warnings: the advance path is
# best-effort and its failures were DEBUG-silent until 2026-08-01, which let
# the durable floor die unnoticed on at least two production conversations
# (cachePrefixHWM absent despite daily warm rounds — L1's guard then had no
# durable backstop). A failure here DEGRADES protection (§2.2: degraded →
# warning) but must not spam error.log on a strained DB — once per conv per
# window.
_WARN_THROTTLE_S = 600.0
_warn_last: dict[tuple[int, str], float] = {}


def _owner_key(conv_id: str, user_id: int) -> tuple[int, str]:
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id < 1:
        raise ValueError('user_id must be a positive integer')
    return user_id, conv_id


def _conversation_settings(conv_id: str, *, user_id: int) -> dict:
    """Read one owner's settings from the semantic storage authority."""
    from lib.storage import get_storage_client

    document = get_storage_client().query(
        'conversation.get', {
            'conv_id': conv_id,
            'user_id': user_id,
            'derive_messages': False,
        })
    return dict((document or {}).get('metadata', {}).get('settings') or {})


def _warn_throttled(
    conv_id: str, user_id: int, msg: str, *args,
) -> None:
    now = time.time()
    key = _owner_key(conv_id, user_id)
    with _hwm_lock:
        last = _warn_last.get(key, 0.0)
        if now - last < _WARN_THROTTLE_S:
            return
        _warn_last[key] = now
    logger.warning(msg, *args)


def read_persisted_boundary(conv_id: str, *, user_id: int) -> int:
    """Return the durable high-water prefix boundary for ``conv_id`` (0 if
    none / unavailable). Cheap: served from a short TTL cache, hitting the DB
    at most once per ``_HWM_TTL_S`` per conv.

    Best-effort — any error returns 0 (degrade to the in-memory path).
    """
    if not conv_id:
        return 0
    key = _owner_key(conv_id, user_id)
    now = time.time()
    with _hwm_lock:
        hit = _hwm_read_cache.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
    val = 0
    try:
        settings = _conversation_settings(conv_id, user_id=user_id)
        cand = settings.get(_HWM_KEY)
        if isinstance(cand, int) and cand > 0:
            val = cand
    except Exception as e:
        logger.debug('[CacheHWM] read failed conv=%s: %s', conv_id[:8], e)
        val = 0
    with _hwm_lock:
        _hwm_read_cache[key] = (val, now + _HWM_TTL_S)
    return val


def advance_persisted_boundary(
    conv_id: str, boundary: int, *, user_id: int,
) -> None:
    """Monotonically raise the durable high-water boundary to ``boundary``.

    Only writes when ``boundary`` strictly exceeds the stored value (so a
    steady-state warm conversation writes the DB just once per genuine growth,
    not every round). Uses the serialized settings RMW so a concurrent writer
    merges rather than clobbers. Best-effort — never raises.
    """
    if not conv_id or boundary <= 0:
        return
    key = _owner_key(conv_id, user_id)
    # Fast-path skip: if our cached read already covers this boundary, the DB
    # value is >= boundary (monotonic) → nothing to write.
    with _hwm_lock:
        hit = _hwm_read_cache.get(key)
    if hit is not None and hit[0] >= boundary and hit[1] > time.time():
        return
    try:
        from lib.conversations.settings_store import update_conversation_settings

        def _mutate(settings: dict) -> Any:
            cur = settings.get(_HWM_KEY)
            cur = cur if isinstance(cur, int) else 0
            if boundary <= cur:
                return False  # nothing to raise — skip the write
            settings[_HWM_KEY] = boundary
            return None

        # notify=False: this is an internal cache-accounting field, NOT a
        # UI-visible setting — must not push a conv_changed frame or reorder
        # the sidebar.
        res = update_conversation_settings(
            conv_id, _mutate, user_id=user_id, notify=False)
        if res is not None:
            # Refresh the read cache so the next fallback sees the new floor
            # immediately (and advance() fast-path-skips until TTL).
            with _hwm_lock:
                _hwm_read_cache[key] = (
                    max(boundary, (_hwm_read_cache.get(key) or (0, 0))[0]),
                    time.time() + _HWM_TTL_S)
    except Exception as e:
        _warn_throttled(conv_id, user_id,
                        '[CacheHWM] advance failed conv=%s: %s — '
                        'durable prefix floor not written (in-memory guard '
                        'still active)', conv_id[:8], e)


def read_last_turn_cache_read(conv_id: str, *, user_id: int) -> int:
    """Return the DURABLE previous-turn final cached-prefix read in tokens
    (0 if none / unavailable).

    THE COLD-STATE FALLBACK for round-1 boundary bucketing. ``turn_boundary_break``
    needs the previous turn's final ``cache_read`` as the baseline to decide
    whether this turn's round-1 collapsed. The primary source
    (``get_prev_turn_cache_read``) scans the in-memory ``_cache_states`` siblings,
    but that memory is wiped by a restart, a >1h stale-eviction, or a
    multi-replica bounce — exactly the cases where the round-1 miss was silently
    laundered into ``no_break``. This persists the baseline on
    ``conversations.settings.lastTurnCacheRead`` so the boundary judgment
    survives a cold process, just like ``read_persisted_boundary`` does for the
    compaction floor.

    Cheap: served from a short TTL cache, hitting the DB at most once per
    ``_HWM_TTL_S`` per conv. Best-effort — any error returns 0 (degrade to the
    memory-only path, i.e. today's behavior).
    """
    if not conv_id:
        return 0
    key = _owner_key(conv_id, user_id)
    now = time.time()
    with _hwm_lock:
        hit = _last_turn_read_cache.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
    val = 0
    try:
        settings = _conversation_settings(conv_id, user_id=user_id)
        cand = settings.get(_LAST_TURN_READ_KEY)
        if isinstance(cand, int) and cand > 0:
            val = cand
    except Exception as e:
        logger.debug('[CacheLastRead] read failed conv=%s: %s', conv_id[:8], e)
        val = 0
    with _hwm_lock:
        _last_turn_read_cache[key] = (val, now + _HWM_TTL_S)
    return val


def write_last_turn_cache_read(
    conv_id: str, cache_read: int, *, user_id: int,
) -> None:
    """Persist the previous-turn final cached-prefix read (tokens).

    LAST-WRITER-WINS (NOT monotonic-max): the baseline must track the real
    latest warm read, so a later floor-only turn lowers it and a warm turn
    raises it. Skips the write when the value is unchanged (steady-state warm
    conversation on the same durable value → no DB churn). Uses the serialized
    settings RMW so a concurrent writer merges rather than clobbers.
    Best-effort — never raises.
    """
    if not conv_id or not isinstance(cache_read, int) or cache_read <= 0:
        return
    key = _owner_key(conv_id, user_id)
    # Fast-path skip: our cached read already equals this value → nothing to write.
    with _hwm_lock:
        hit = _last_turn_read_cache.get(key)
    if hit is not None and hit[0] == cache_read and hit[1] > time.time():
        return
    try:
        from lib.conversations.settings_store import update_conversation_settings

        def _mutate(settings: dict) -> Any:
            cur = settings.get(_LAST_TURN_READ_KEY)
            cur = cur if isinstance(cur, int) else 0
            if cache_read == cur:
                return False  # unchanged — skip the write
            settings[_LAST_TURN_READ_KEY] = cache_read
            return None

        # notify=False: internal cache-accounting field, NOT a UI-visible
        # setting — must not push a conv_changed frame or reorder the sidebar.
        res = update_conversation_settings(
            conv_id, _mutate, user_id=user_id, notify=False)
        if res is not None:
            with _hwm_lock:
                _last_turn_read_cache[key] = (
                    cache_read, time.time() + _HWM_TTL_S)
    except Exception as e:
        _warn_throttled(conv_id, user_id,
                        '[CacheLastRead] write failed conv=%s: %s — '
                        'durable round-1 baseline not written',
                        conv_id[:8], e)


def _reset_read_cache_for_tests() -> None:
    """Test hook: clear the in-process read caches so a test can observe a fresh
    DB read (used by the restart-simulation NEUTER)."""
    with _hwm_lock:
        _hwm_read_cache.clear()
        _last_turn_read_cache.clear()


__all__ = [
    'read_persisted_boundary',
    'advance_persisted_boundary',
    'read_last_turn_cache_read',
    'write_last_turn_cache_read',
    '_reset_read_cache_for_tests',
]

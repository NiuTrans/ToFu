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

# Read-cache TTL: the cold-thread fallback reads at most once per this window.
_HWM_TTL_S = 30.0

# conv_id → (value, expires_at). Guarded by _hwm_lock.
_hwm_read_cache: dict[str, tuple[int, float]] = {}
_hwm_lock = threading.Lock()


def read_persisted_boundary(conv_id: str) -> int:
    """Return the durable high-water prefix boundary for ``conv_id`` (0 if
    none / unavailable). Cheap: served from a short TTL cache, hitting the DB
    at most once per ``_HWM_TTL_S`` per conv.

    Best-effort — any error returns 0 (degrade to the in-memory path).
    """
    if not conv_id:
        return 0
    now = time.time()
    with _hwm_lock:
        hit = _hwm_read_cache.get(conv_id)
        if hit is not None and hit[1] > now:
            return hit[0]
    val = 0
    try:
        from lib.database import DOMAIN_CHAT, get_thread_db
        from lib.utils import safe_json
        db = get_thread_db(DOMAIN_CHAT)
        row = db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, 1),
        ).fetchone()
        if row is not None:
            try:
                raw = row['settings']
            except (TypeError, KeyError, IndexError):
                raw = row[0] if row else None
            settings = safe_json(raw, default={}, label='cache_hwm')
            if isinstance(settings, dict):
                cand = settings.get(_HWM_KEY)
                if isinstance(cand, int) and cand > 0:
                    val = cand
    except Exception as e:
        logger.debug('[CacheHWM] read failed conv=%s: %s', conv_id[:8], e)
        val = 0
    with _hwm_lock:
        _hwm_read_cache[conv_id] = (val, now + _HWM_TTL_S)
    return val


def advance_persisted_boundary(conv_id: str, boundary: int) -> None:
    """Monotonically raise the durable high-water boundary to ``boundary``.

    Only writes when ``boundary`` strictly exceeds the stored value (so a
    steady-state warm conversation writes the DB just once per genuine growth,
    not every round). Uses the serialized settings RMW so a concurrent writer
    merges rather than clobbers. Best-effort — never raises.
    """
    if not conv_id or boundary <= 0:
        return
    # Fast-path skip: if our cached read already covers this boundary, the DB
    # value is >= boundary (monotonic) → nothing to write.
    with _hwm_lock:
        hit = _hwm_read_cache.get(conv_id)
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
            conv_id, _mutate, notify=False)
        if res is not None:
            # Refresh the read cache so the next fallback sees the new floor
            # immediately (and advance() fast-path-skips until TTL).
            with _hwm_lock:
                _hwm_read_cache[conv_id] = (
                    max(boundary, (_hwm_read_cache.get(conv_id) or (0, 0))[0]),
                    time.time() + _HWM_TTL_S)
    except Exception as e:
        logger.debug('[CacheHWM] advance failed conv=%s: %s', conv_id[:8], e)


def _reset_read_cache_for_tests() -> None:
    """Test hook: clear the in-process read cache so a test can observe a fresh
    DB read (used by the restart-simulation NEUTER)."""
    with _hwm_lock:
        _hwm_read_cache.clear()


__all__ = [
    'read_persisted_boundary',
    'advance_persisted_boundary',
    '_reset_read_cache_for_tests',
]

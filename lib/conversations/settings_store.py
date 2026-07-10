"""lib.conversations.settings_store — serialized read-merge-write for the
``conversations.settings`` JSON column.

WHY
---
~13 call sites historically did a bare read-modify-write of the WHOLE settings
blob (``SELECT settings`` → ``json.loads`` → mutate one key → ``UPDATE
conversations SET settings=?``). Because each rewrites the ENTIRE blob, two
concurrent settings writers silently clobber each other's keys — the last
writer wins and the other's mutation is lost. Real collisions on a single box:

  * a tool-toggle ``PATCH /chat/tool-state`` (worker thread) vs. the queue
    dispatcher stamping ``activeTaskId`` (dispatch thread) — a dropped
    ``activeTaskId`` orphans the SSE re-attach;
  * an autopilot summary store (``autopilotSummaries``, task thread) vs. a
    project-summary cache write (``projectSummary``, daemon thread) — a lost
    run record or a wiped summary.

Unlike the messages column — guarded by
``DefaultConversationStore.cas_update_conversation_messages`` — the settings
column had NO shared write seam, so every site reinvented the racy RMW.

WHY NOT CAS-on-updated_at
-------------------------
The messages path CAS-guards on ``updated_at``. That is the WRONG tool for
settings: settings-only writers deliberately DO NOT bump ``updated_at`` (a tool
toggle must not reorder the sidebar). So instead this module SERIALIZES the
read+mutate+write per ``conv_id`` behind an in-process lock, so every writer
merges its key onto the FRESHEST blob rather than an already-stale snapshot.

Scope
-----
This is a SINGLE-BOX mechanism (the process owns its DB connections). Horizontal
scale-out is a separately-parked epic; when it lands, this seam is where the
per-conv serialization would move onto the shared lease store — callers already
route through ``update_conversation_settings`` so only this module changes.
"""

from __future__ import annotations

import json
import threading
import weakref
from typing import Any, Callable

from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db
from lib.log import get_logger
from lib.utils import safe_json

logger = get_logger(__name__)

DEFAULT_USER_ID = 1  # single-user chatui convention (mirrors routes.common)

# Per-conv locks, GC'd when no writer holds one. A WeakValueDictionary keeps
# the map from growing unbounded across the lifetime of the process: the Lock
# survives only while a caller (the `with _lock_for(...)` frame) holds a strong
# reference to it; two concurrent writers for the SAME conv see the same live
# lock (the first caller's frame keeps it in the map), so mutual exclusion
# holds; once both release and drop their refs the entry is collected.
_locks_guard = threading.Lock()
_locks: "weakref.WeakValueDictionary[str, threading.Lock]" = weakref.WeakValueDictionary()


def _lock_for(conv_id: str) -> threading.Lock:
    with _locks_guard:
        lk = _locks.get(conv_id)
        if lk is None:
            lk = threading.Lock()
            _locks[conv_id] = lk
        return lk


def _row_settings(row: Any) -> Any:
    """Extract the raw settings value from a DB row (key- or index-access)."""
    try:
        return row['settings']
    except (TypeError, KeyError, IndexError) as e:
        logger.debug('[SettingsStore] settings key access failed, trying index: %s', e)
        try:
            return row[0]
        except (TypeError, KeyError, IndexError) as e2:
            logger.debug('[SettingsStore] settings index access failed (none): %s', e2)
            return None


def update_conversation_settings(
    conv_id: str,
    mutate: Callable[[dict], Any],
    *,
    user_id: int = DEFAULT_USER_ID,
    db: Any = None,
) -> dict | None:
    """Serialized read-merge-write of a conversation's ``settings`` JSON.

    Acquires a per-``conv_id`` lock, then — under that lock — RE-READS the
    current settings, invokes ``mutate(settings)`` (which should mutate the
    dict in place), and writes the result back. Because the read and the write
    are both inside the lock, a concurrent settings writer for the same
    conversation merges onto the freshest blob instead of clobbering it.

    Does NOT touch ``messages`` / ``updated_at`` / ``msg_count`` — this is a
    settings-only write and must not reorder the sidebar.

    Args:
        conv_id: Conversation id.
        mutate: Callback invoked with the freshly-parsed settings dict. Mutate
            in place. Return ``False`` to signal "nothing changed — skip the
            write" (e.g. a conditional set that found the key already present);
            any other return value (including ``None``) proceeds with the
            write. Use a closure to hand a computed value back to the caller.
        user_id: Owner id (single-user default).
        db: Optional pooled/thread-local connection to reuse. When ``None`` the
            thread-local chat connection is used (safe from worker threads).

    Returns:
        The (post-mutate) settings dict, or ``None`` when the conversation row
        is absent (caller treats that as "skipped — conv not persisted yet").
    """
    if not conv_id:
        return None
    _db = db if db is not None else get_thread_db(DOMAIN_CHAT)
    with _lock_for(conv_id):
        row = _db.execute(
            'SELECT settings FROM conversations WHERE id=? AND user_id=?',
            (conv_id, user_id),
        ).fetchone()
        if not row:
            return None
        settings = safe_json(_row_settings(row), default={}, label='settings_store')
        if not isinstance(settings, dict):
            settings = {}
        res = mutate(settings)
        if res is False:
            return settings
        db_execute_with_retry(
            _db,
            'UPDATE conversations SET settings=? WHERE id=? AND user_id=?',
            (json.dumps(settings, ensure_ascii=False), conv_id, user_id),
        )
        return settings


def set_conversation_settings(
    conv_id: str,
    updates: dict,
    *,
    user_id: int = DEFAULT_USER_ID,
    db: Any = None,
) -> dict | None:
    """Convenience: merge a flat dict of key→value ``updates`` into settings.

    Thin wrapper over :func:`update_conversation_settings` for the common
    "set these keys" case (``dict.update`` returns ``None`` → the write always
    proceeds). Returns the same value as the underlying helper.
    """
    if not updates:
        # Read-only no-op mutation: never lose the write path's row-absent
        # semantics, but skip the UPDATE (nothing to merge).
        return update_conversation_settings(
            conv_id, lambda _s: False, user_id=user_id, db=db)
    return update_conversation_settings(
        conv_id, lambda s: s.update(updates), user_id=user_id, db=db)


__all__ = ['update_conversation_settings', 'set_conversation_settings']

"""routes/chat_state.py — process-local send-abort marker (chat.py slice 2).

**Extraction context** (board epic ``pt_04686ac6054a451e``, slice 2 of N):
This module holds the ONE piece of module-level mutable state
``routes/chat.py`` owned outside its route handlers — a small dict recording,
per conversation, the wall-clock timestamp at which an
``/api/chat/abort-conv/<conv_id>`` request last fired. The chat_send flow
consults it AFTER an auto-translate round-trip to detect the
"user hit abort while we were blocked in translation" race and drop the
would-be phantom send instead of dispatching it.

Kept intentionally SEPARATE from ``routes/chat_helpers.py`` (which is
documented as strictly-pure, no module-level state). Mixing process-local
mutable state into a "helpers" grab-bag would violate that invariant and
mislead future readers about what belongs where.

  * ``_send_abort_marker``       — dict {conv_id: wall_clock_seconds}
  * ``_send_abort_marker_lock``  — threading.Lock protecting the dict
  * ``_mark_conv_aborted(conv_id)``  — record NOW as the conv's abort ts
  * ``_was_aborted_after(conv_id, since_ts)`` — did an abort land after since_ts?

``routes/chat.py`` re-exports every name from this module so all existing
``from routes.chat import _mark_conv_aborted`` / ``_was_aborted_after``
call sites (and any future external test) keep working. Wire-parity guarded
by ``tests/test_routes_chat_wire_parity.py``.

**Scope note**: process-local, not distributed. On a multi-replica shard
another replica will not see this marker; the /api/chat/abort-conv handler
that mutates it also runs the ``abort_running_tasks_for_conv`` broadcast,
which is the cross-replica signal. This marker is ONLY the same-process
race hint between abort-conv and a still-translating send in the SAME
worker's memory.
"""

from __future__ import annotations

import threading
import time


# conv_id → abort wall_clock_seconds. Never grows unbounded in practice:
# a conv id is 12 hex chars, entries are keyed by the specific conv the user
# aborted, and there is no cleanup because a stale entry is HARMLESS (the
# ``since_ts`` guard rejects any lookup whose reference timestamp is newer
# than the stored one — i.e. after any subsequent successful send, the
# marker becomes a no-op for that conv). Bounded by the number of distinct
# convs the user has aborted since process start; a leak-in-name-only.
_send_abort_marker: dict[str, float] = {}
_send_abort_marker_lock = threading.Lock()


def _mark_conv_aborted(conv_id: str) -> None:
    """Record that this conv was aborted at wall clock ``time.time()``."""
    if not conv_id:
        return
    with _send_abort_marker_lock:
        _send_abort_marker[conv_id] = time.time()


def _was_aborted_after(conv_id: str, since_ts: float | None) -> bool:
    """Return True if /api/chat/abort-conv ran for this conv after ``since_ts``."""
    if not conv_id or since_ts is None:
        return False
    with _send_abort_marker_lock:
        ts = _send_abort_marker.get(conv_id)
    return ts is not None and ts >= since_ts


__all__ = [
    '_send_abort_marker',
    '_send_abort_marker_lock',
    '_mark_conv_aborted',
    '_was_aborted_after',
]

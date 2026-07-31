"""Shared singleton state for the ``lib.tasks_pkg.manager`` facade package.

This module is the SINGLE HOME of the process-wide task state that every
manager sub-module reads/writes:

  * ``_chat_runtime`` — the backing :class:`~lib.task_runtime.TaskRuntime`.
  * ``tasks`` / ``tasks_lock`` — ALIASES to the runtime's internal storage
    (``_chat_runtime._tasks`` / ``_chat_runtime._lock``). 47+ import sites across
    routes/, lib/, and tests/ reference these directly and MUST observe THE SAME
    object, so they are defined exactly once here and re-exported unchanged from
    the package ``__init__``.
  * ``_conv_latest_task`` / ``_conv_latest_task_lock`` — the freshness-guard
    conv→latest-task index + its lock.
  * ``_LATEST_KIND`` / ``_LATEST_TTL`` — cross-replica supersede-index knobs.
  * ``CHECKPOINT_MIN_DELTA_CHARS`` — partial-checkpoint coalescing threshold.
  * ``_record_latest_task`` / ``_latest_task_for_conv`` — the index accessors.

Keeping all of this in one leaf module (no sibling imports) means the aliasing
identity is preserved and there is no risk of two modules each minting their own
copy of the shared dict.
"""

import os
import threading

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)


# ── Backing runtime ──────────────────────────────────────────────
# kind='chat'. push_channel='chat' (matches the existing /api/push routes
# and the frontend ``pushSubscribe('chat', taskId)`` consumer).
# ttl=3600 matches the legacy cleanup_old_tasks threshold.
_chat_runtime = TaskRuntime(
    'chat', ttl=3600,
    push_channel='chat',
    error_source='lib.tasks_pkg.manager',
)

# ── Module-level compatibility exports ─────────────────────────────
# Both names alias the runtime's internals so the 47 existing call sites
# (routes/chat.py, routes/endpoint.py, lib/agent_backends/builtin.py, etc.)
# continue to work without modification.
tasks = _chat_runtime._tasks  # type: ignore[attr-defined]
tasks_lock = _chat_runtime._lock  # type: ignore[attr-defined]

# ── Conversation → latest task_id mapping for freshness guard ──
# When a new task starts for a conv, the old task becomes stale and its
# _sync_result_to_conversation writes should be rejected.
_conv_latest_task = {}   # conv_id → task_id
_conv_latest_task_lock = threading.Lock()

# ── Cross-replica supersede index (Epic C §4.3) ──
# The freshness guard's "newest task for this conv" must be authoritative
# ACROSS replicas so a stale task on replica A recognises that replica B
# started a newer task for the same conv. We MIRROR conv->latest_task_id into
# the shared runtime_state_store: under inproc the local dict stays the fast
# authoritative path (byte-identical to before); under redis the store is the
# fleet source of truth. The actual cross-replica ABORT of the superseded task
# routes to its owning replica via taskId affinity (LB concern) — this index
# only decides WHO is newest.
_LATEST_KIND = 'latest'
_LATEST_TTL = 3600.0  # a conv's latest-task marker; refreshed on each new task

# ── Partial-checkpoint coalescing (§10.1 hyperparameter) ──
# Minimum content+thinking growth (chars) since the last conversations.messages
# write before a mid-stream partial checkpoint bothers rewriting that whole
# O(conv-size) JSON blob again. Small deltas are COALESCED (skipped), not
# dropped: the delta is measured against the DB row, so a skip leaves the row
# stale and the NEXT delta's measured growth includes the skipped chars — it is
# inherently cumulative and always flushes once growth crosses the threshold.
# The per-task task_results checkpoint (the cheap blob) is written EVERY
# checkpoint regardless, and the terminal _sync_result_to_conversation always
# writes the full final content — so the messages row is a derived mirror that
# may lag by < this many chars mid-stream and always converges at completion.
# The reconnect / poll-fallback reload path reads task_results + the task_events
# log (never this row) so it is unaffected. 0 disables coalescing (write on
# every delta — the legacy behaviour). Override with CHECKPOINT_MIN_DELTA_CHARS.
try:
    CHECKPOINT_MIN_DELTA_CHARS = int(os.environ.get('CHECKPOINT_MIN_DELTA_CHARS', '160'))
    if CHECKPOINT_MIN_DELTA_CHARS < 0:
        CHECKPOINT_MIN_DELTA_CHARS = 0
except (ValueError, TypeError) as _e:
    logger.debug('[Checkpoint] CHECKPOINT_MIN_DELTA_CHARS parse failed, using default: %s', _e)
    CHECKPOINT_MIN_DELTA_CHARS = 160


def _record_latest_task(conv_id: str, task_id: str) -> None:
    with _conv_latest_task_lock:
        _conv_latest_task[conv_id] = task_id
    try:
        from lib.runtime_state_store import get_store
        get_store().set_value(_LATEST_KIND, conv_id, task_id, _LATEST_TTL)
    except Exception as e:
        logger.debug('[Task] supersede index mirror failed conv=%s: %s',
                     conv_id[:8], e)


def _latest_task_for_conv(conv_id: str):
    """Fleet-authoritative newest task_id for a conv. Prefers the shared store
    (cross-replica) and falls back to the local dict; the two agree under the
    inproc backend."""
    try:
        from lib.runtime_state_store import get_store
        v = get_store().get_value(_LATEST_KIND, conv_id)
        if v:
            return v
    except Exception as e:
        logger.debug('[Task] supersede index read failed conv=%s: %s',
                     conv_id[:8], e)
    with _conv_latest_task_lock:
        return _conv_latest_task.get(conv_id)


def _live_successor_task_id(conv_id: str, exclude_task_id: str = '') -> str:
    """The conv's supersede-index successor, iff it is a DIFFERENT live task.

    Ships the conv→latest-task index onto terminal SSE frames (the LATE-done
    synthesis in ``lib.chat_dispatch`` and the real ``done`` in
    ``orchestrator/_finalize.py``) as ``latestLiveTaskId``, so the client's
    terminal-continuation attach reducer can hop to the successor the
    autopilot hook already spawned — the VU sub-task is a carrier, invisible
    to ``/api/v1/chat/active``, so without this stamp the client has NO way
    to discover it (production 2026-07-25: parent stream closed at turn end,
    the VU ran invisibly for minutes, a queued send sat silent until manual
    refresh).

    Returns '' when the index is absent, points at the dying task itself
    (the normal no-successor case), or names a task that is no longer live
    (terminal / aborted / evicted from the registry). Best-effort: any
    probe failure yields '' (no stamp), never raises into a stream tick.
    """
    if not conv_id:
        return ''
    try:
        succ = _latest_task_for_conv(conv_id)
        if not succ or succ == exclude_task_id:
            return ''
        t = _chat_runtime.get(succ)
        if not t or t.get('status') not in ('pending', 'running') or t.get('aborted'):
            return ''
        return succ
    except Exception as e:
        logger.debug('[Task] live-successor probe failed conv=%s: %s',
                     conv_id[:8], e)
        return ''


def _live_successor_info(conv_id: str, exclude_task_id: str = '') -> tuple[str, bool]:
    """``_live_successor_task_id`` + the successor's VU-carrier flag.

    The frontend needs to know WHETHER the hop target named by
    ``latestLiveTaskId`` is a VU carrier: a VU successor must be attached
    through the VU connector (detached dummy assistant, no "Agent"
    placeholder bubble — the carrier emits only the ``autopilot_vu_*``
    contract), while a worker successor goes through the normal path.
    Terminal SSE frames therefore also stamp ``latestLiveTaskIsVu`` when
    this returns ``(task_id, True)``.

    Returns ``(task_id, is_vu_carrier)``; ``('', False)`` exactly when
    ``_live_successor_task_id`` would return ``''``.
    """
    succ = _live_successor_task_id(conv_id, exclude_task_id)
    if not succ:
        return '', False
    try:
        t = _chat_runtime.get(succ)
        return succ, bool(t and t.get('_vu_subtask'))
    except Exception as e:
        logger.debug('[Task] live-successor is-vu probe failed conv=%s: %s',
                     conv_id[:8], e)
        return succ, False

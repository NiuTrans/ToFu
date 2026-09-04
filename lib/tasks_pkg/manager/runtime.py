"""Chat task runtime and conversation-liveness indexes.

This module is the SINGLE HOME of the process-wide task state that every
manager sub-module reads/writes:

  * ``chat_task_runtime`` — the backing
    :class:`~lib.agent_core.task_runtime.TaskRuntime`; its registry map and lock
    are never exported.
  * ``_conv_latest_task`` / ``_conv_latest_task_lock`` — the freshness-guard
    conv→latest-task index + its lock.
  * ``_LATEST_KIND`` / ``_LATEST_TTL`` — cross-replica supersede-index knobs.
  * ``CHECKPOINT_MIN_DELTA_CHARS`` — partial-checkpoint coalescing threshold.
  * ``_record_latest_task`` / ``_latest_task_for_conv`` — the index accessors.
  * ``push_withheld_for_conv`` — read-side delivery-wedge probe consumed by
    the conversation-sync snapshot/SSE heartbeat routes.

Keeping all of this in one leaf module (no sibling imports) prevents duplicate
runtime or freshness-index authorities.
"""

import os
import threading

from lib.log import get_logger
from lib.agent_core.task_runtime import TaskRuntime
from lib.agent_core.task_runtime_policy import (
    resolve_chat_task_terminal_ttl_seconds,
)

logger = get_logger(__name__)


# ── Backing runtime ──────────────────────────────────────────────
# kind='chat'. push_channel='chat' (matches the existing /api/push routes
# and the frontend ``pushSubscribe('chat', taskId)`` consumer).
# Terminal state survives through owner-scoped task-results + event cold
# replay, so the hot Python dictionary needs only the launch-derived late
# poller window. Active tasks are never TTL-evicted by TaskRuntime.
# TODO(enterprise, R1): live task/event registry is single-process memory —
# relay per-task frames on the push bus so any replica can serve
# SSE/poll/abort. docs/ENTERPRISE_READINESS_AUDIT.md
CHAT_TASK_TERMINAL_TTL_SECONDS = resolve_chat_task_terminal_ttl_seconds()
chat_task_runtime = TaskRuntime(
    'chat', ttl=CHAT_TASK_TERMINAL_TTL_SECONDS,
    push_channel='chat',
    error_source='lib.tasks_pkg.manager',
)

# ── Conversation → latest task_id mapping for freshness guard ──
# When a new task starts for a conv, the old task becomes stale and its
# stale task events should be rejected by the bound attempt fence.
_conv_latest_task = {}   # conv_id → task_id
_conv_latest_task_lock = threading.Lock()

# ── Abort tombstones ( ③-3) ──
# An abort that arrives while the target task is MISSING from ``tasks`` (the
# 2026-08-01 evaporation: live carrier/worker unreachable by every abort
# endpoint — 404 / aborted:0 while the thread kept cycling) is recorded here
# instead of being dropped. The running task's abort_check
# (``make_task_abort_check``) consults this set every retry cycle, so the
# abort signal reaches the worker even when the registry lost it. Ids are
# uuids — no reuse — and tombstones are only minted on registry-miss aborts,
# so the set stays tiny; a hard cap guards the pathological case.
_abort_tombstones = set()   # task_id
_abort_tombstones_lock = threading.Lock()
_ABORT_TOMBSTONES_CAP = 1024

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
# checkpoint regardless, and the terminal turn event always
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

# Above this stored transcript size, a mid-stream checkpoint persists only the
# task-result/event-log recovery record; an authoritative Turn owns structural
# segments for conversation attempts. Rewriting a multi-megabyte
# conversations.messages JSON value every five seconds creates large Python
# objects plus PostgreSQL TOAST/WAL churn; terminal sync still writes the full
# settled conversation once. 0 restores the legacy unlimited behaviour.
try:
    CHECKPOINT_CONV_BLOB_MAX_BYTES = int(
        os.environ.get('CHECKPOINT_CONV_BLOB_MAX_BYTES', str(2 * 1024 * 1024)))
    if CHECKPOINT_CONV_BLOB_MAX_BYTES < 0:
        CHECKPOINT_CONV_BLOB_MAX_BYTES = 0
except (ValueError, TypeError) as _e:
    logger.debug('[Checkpoint] CHECKPOINT_CONV_BLOB_MAX_BYTES parse failed, '
                 'using default: %s', _e)
    CHECKPOINT_CONV_BLOB_MAX_BYTES = 2 * 1024 * 1024


def _record_latest_task(conv_id: str, task_id: str) -> None:
    with _conv_latest_task_lock:
        _conv_latest_task[conv_id] = task_id
    try:
        from lib.runtime_state_store import get_store
        get_store().set_value(_LATEST_KIND, conv_id, task_id, _LATEST_TTL)
    except Exception as e:
        logger.debug('[Task] supersede index mirror failed conv=%s: %s',
                     conv_id[:8], e)


def _clear_latest_task(conv_id: str, *, expect_task_id: str | None = None) -> bool:
    """Clear a conv's latest-task pointer in BOTH the local dict and the
    store mirror. Returns True when the local entry was removed.

    Every deletion of the local entry MUST go through here:
    ``_record_latest_task`` dual-writes the store mirror (TTL 1h), so a
    local-only delete leaves the store-backed ``_latest_task_for_conv``
    returning the corpse for up to an hour — a discarded VU carrier keeps
    "owning" the conv on every store-backed freshness read (the msb6ohqi
    2026-08-02 stall class). With ``expect_task_id`` the local entry is
    cleared only when it still names that task (the compare-and-delete
    discipline discard_task already had); the mirror is invalidated
    unconditionally — the store offers no compare-and-delete, and a
    wrong-mirror delete only costs one read falling back to the local dict.
    """
    removed = False
    with _conv_latest_task_lock:
        if expect_task_id is None:
            removed = _conv_latest_task.pop(conv_id, None) is not None
        elif _conv_latest_task.get(conv_id) == expect_task_id:
            del _conv_latest_task[conv_id]
            removed = True
    try:
        from lib.runtime_state_store import get_store
        get_store().delete_value(_LATEST_KIND, conv_id)
    except Exception as e:
        logger.debug('[Task] supersede index mirror clear failed conv=%s: %s',
                     conv_id[:8], e)
    return removed


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
        t = chat_task_runtime.get(succ)
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
        t = chat_task_runtime.get(succ)
        return succ, bool(t and t.get('_vu_subtask'))
    except Exception as e:
        logger.debug('[Task] live-successor is-vu probe failed conv=%s: %s',
                     conv_id[:8], e)
        return succ, False


def push_withheld_for_conv(conv_id: str) -> dict | None:
    """Delivery-wedge probe for a conversation's live task.

    ``TaskRuntime.append_event`` stamps ``_pushWithheldAt``/``_pushWithheldCount``
    on the task dict while authoritative frames are being withheld
    (durable-before-visible: persistence failed, so nothing may reach the
    client). The withheld frames cannot carry this signal themselves — the
    write path is exactly what is broken — so the conversation-sync read
    paths (sync snapshot + SSE heartbeat) poll this probe instead.

    Returns ``{'taskId': ..., 'sinceMs': ..., 'count': ...}`` while the
    conv's latest task is live (pending/running) with withheld pushes, else
    None. Best-effort like the successor probes: any lookup failure yields
    None, never raises into a request/stream tick.
    """
    if not conv_id:
        return None
    try:
        task_id = _latest_task_for_conv(conv_id)
        if not task_id:
            return None
        task = chat_task_runtime.get(task_id)
        if not task or task.get('status') not in ('pending', 'running'):
            return None
        withheld_at = task.get('_pushWithheldAt')
        if not withheld_at:
            return None
        return {
            'taskId': task_id,
            'sinceMs': int(float(withheld_at) * 1000),
            'count': int(task.get('_pushWithheldCount') or 0),
        }
    except Exception as e:
        logger.debug('[Task] push-withheld probe failed conv=%s: %s',
                     conv_id[:8], e)
        return None

"""Report task store — server-owned background generation.

Design (per user request 2026-04-18):
  • Report generation happens ONCE per (paper_hash, lang). If a task is
    already running, any new /start request joins it and polls the same
    events.
  • Task is server-owned: tool-call progress, deltas, status are all
    accumulated in an append-only `events` list. Frontend polls
    /api/paper/report/poll?cursor=N and replays — no SSE, no client-held
    state, refresh-safe and tab-switch-safe.
  • Event schema mirrors the chat stream (tool_start / tool_done /
    delta / thinking / done / enriched / error) so the frontend can
    reuse `renderToolRoundsHTML` directly.
  • On completion the enriched report is persisted to the `paper_reports`
    table. Subsequent opens hit the DB cache instantly (no task spawned).
"""

import threading
import time

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)


# Backed by the unified TaskRuntime. Dedup index keys (paper_hash, lang)
# tuples to task_id strings — preserves the "two clients open the same
# paper share one task" semantic.
_report_runtime = TaskRuntime(
    'paper-report', ttl=3600,
    push_channel='paper',
    error_source='routes.paper:report',
)
# (phash, lang) → task_id. Updated alongside _report_runtime.create()/cleanup.
_report_dedup_index: dict[tuple, str] = {}
_report_dedup_lock = threading.Lock()
_REPORT_TASK_TTL = 3600


def _report_index_get(phash: str, lang: str) -> dict | None:
    """Find a task by (paper_hash, lang). Returns the task dict or None."""
    with _report_dedup_lock:
        tid = _report_dedup_index.get((phash, lang))
    if not tid:
        return None
    return _report_runtime.get(tid)


def _report_index_register(phash: str, lang: str, task_id: str) -> None:
    """Register a (phash, lang) → task_id mapping in the dedup index."""
    with _report_dedup_lock:
        _report_dedup_index[(phash, lang)] = task_id


def _new_report_task(task_id, phash, lang, model, *, client_title='', ui_lang='',
                     config=None):
    """Create a fresh report task. Registers it in the dedup index.

    ``lang`` is the cache key (plain 'en'/'zh' for reports, or the composite
    ``review:<venue>:<uilang>`` for Review Mode). ``ui_lang`` is the REAL UI
    language ('en'/'zh') the engine uses for image-injection / appendix
    headings; defaults to ``lang`` when not given (ordinary report path).
    ``config`` is the caller cfg dict (may be None) — the insight second-pass
    reads ``paperInsightPersonalContext`` from it via personal_scope to decide
    whether the operator's personal reader-context may be injected.
    """
    task = _report_runtime.create(
        task_id=task_id,
        meta={'paper_hash': phash, 'lang': lang, 'model': model},
    )
    # Augment with legacy field names that the rest of paper.py expects.
    task.update({
        'task_id': task_id,
        'paper_hash': phash,
        'lang': lang,
        'ui_lang': ui_lang or lang,
        'model': model,
        'client_title': client_title,
        'config': config,
        'status': 'pending',        # pending → running → done | error
        'finished_at': None,
        'full_text': '',            # accumulated delta text
        'enriched_text': '',        # final enriched text (with images)
        'tool_rounds': [],          # synchronised toolRounds array
        'round_counter': 0,
        # Note: TaskRuntime stores 'error' as None; legacy code expected ''.
        # Tests/code that read task['error'] should treat both as "no error".
    })
    _report_index_register(phash, lang, task_id)
    return task


def _append_report_event(task, event):
    """Append an event to the task's event log. Thread-safe.

    Every event gets a monotonic `seq` so pollers can resume from a cursor.
    Also pushes to the global WebSocket channel for real-time delivery
    (handled by TaskRuntime when push_channel='paper').
    """
    _report_runtime.append_event(task['task_id'], event)


def _cleanup_stale_report_tasks():
    """Drop finished tasks older than TTL and remove their dedup entries."""
    # Snapshot finished tasks BEFORE cleanup to know which dedup keys to drop.
    finished_ids: set = set()
    with _report_runtime._lock:
        for tid, t in _report_runtime._tasks.items():
            if t['status'] in ('done', 'error', 'aborted'):
                if t.get('finished_at'):
                    if time.time() - t['finished_at'] > _REPORT_TASK_TTL:
                        finished_ids.add(tid)
    n = _report_runtime.cleanup_stale()
    if n:
        # Drop matching dedup-index entries
        with _report_dedup_lock:
            stale_keys = [k for k, tid in _report_dedup_index.items()
                          if tid in finished_ids]
            for k in stale_keys:
                _report_dedup_index.pop(k, None)
        logger.debug('[Paper:Report] Cleaned %d stale task(s)', n)


# Compatibility shims (legacy code in paper.py / tests still references these names).
_report_tasks = _report_runtime._tasks       # type: ignore[attr-defined]
_report_tasks_lock = _report_runtime._lock   # type: ignore[attr-defined]

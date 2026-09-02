"""Report task store — server-owned background generation.

Design (per user request 2026-04-18):
  • Ordinary report generation happens once per (paper_hash, lang). In-flight
    reuse additionally requires an exact model+config fingerprint, so paired
    experiment arms cannot join one another.
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

from lib.log import get_logger
from lib.agent_core.task_runtime import TaskRuntime
from lib.paper.request_policy import paper_request_policy_telemetry

logger = get_logger(__name__)


# Backed by the unified TaskRuntime. Dedup index keys include the request's
# execution fingerprint, so two clients share work only when model and config
# are identical.  This prevents concurrent experiment arms from joining one
# another while preserving the ordinary "same paper shares one task" path.
_report_runtime = TaskRuntime(
    'paper-report', ttl=3600,
    push_channel='paper',
    error_source='routes.paper:report',
)
# (owner, phash, lang, execution_fingerprint) → task_id.
# Updated alongside _report_runtime.create()/cleanup.
_report_dedup_index: dict[tuple, str] = {}
_report_dedup_lock = threading.Lock()
_REPORT_TASK_TTL = 3600


def _report_index_get(
    phash: str,
    lang: str,
    *,
    user_id: int,
    execution_fingerprint: str = '',
) -> dict | None:
    """Find an owner's exact request task, or latest compatible task.

    Routes that may join work pass ``execution_fingerprint`` and therefore
    require an exact model/config match.  The omitted-fingerprint form remains
    for the tab-reentry lookup API: it returns the most recently registered
    task for the paper regardless of request policy, but never joins it.
    """
    with _report_dedup_lock:
        if execution_fingerprint:
            tid = _report_dedup_index.get(
                (user_id, phash, lang, execution_fingerprint))
        else:
            tid = next((
                candidate_tid
                for key, candidate_tid in reversed(
                    tuple(_report_dedup_index.items()))
                if key[:3] == (user_id, phash, lang)
            ), None)
    if not tid:
        return None
    return _report_runtime.get_owned(tid, user_id=user_id)


def _report_index_register(
    phash: str, lang: str, task_id: str, *, user_id: int,
    execution_fingerprint: str = '',
) -> None:
    """Register an owner- and request-policy-scoped report mapping."""
    if not execution_fingerprint:
        task = _report_runtime.get_owned(task_id, user_id=user_id) or {}
        execution_fingerprint = str(task.get('execution_fingerprint') or '')
    if not execution_fingerprint:
        raise ValueError('report execution fingerprint is required')
    with _report_dedup_lock:
        _report_dedup_index[
            (user_id, phash, lang, execution_fingerprint)
        ] = task_id


def _new_report_task(task_id, phash, lang, model, *, client_title='', ui_lang='',
                     config=None, user_id: int):
    """Create a fresh report task. Registers it in the dedup index.

    ``lang`` is the cache key (plain 'en'/'zh' for reports, or the composite
    ``review:<venue>:<uilang>`` for Review Mode). ``ui_lang`` is the REAL UI
    language ('en'/'zh') the engine uses for image-injection / appendix
    headings; defaults to ``lang`` when not given (ordinary report path).
    ``config`` is the caller cfg dict (may be None) — the insight second-pass
    reads ``paperInsightPersonalContext`` from it via personal_scope to decide
    whether the operator's personal reader-context may be injected.
    """
    detached_config = dict(config or {})
    request_policy = paper_request_policy_telemetry(
        model=model, config=detached_config)
    execution_fingerprint = request_policy['executionFingerprint']
    task = _report_runtime.create(
        user_id=user_id,
        task_id=task_id,
        meta={
            'paper_hash': phash,
            'lang': lang,
            'model': model,
            'execution_fingerprint': execution_fingerprint,
        },
    )
    _report_runtime.update_fields(task_id, fields={
        'task_id': task_id,
        'paper_hash': phash,
        'lang': lang,
        'ui_lang': ui_lang or lang,
        'model': model,
        'client_title': client_title,
        'config': detached_config,
        'execution_fingerprint': execution_fingerprint,
        'requestPolicyV1': request_policy,
        'full_text': '',            # accumulated delta text
        'enriched_text': '',        # final enriched text (with images)
        'tool_rounds': [],          # synchronised toolRounds array
        'round_counter': 0,
    })
    _report_index_register(
        phash, lang, task_id, user_id=user_id,
        execution_fingerprint=execution_fingerprint)
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
    n = _report_runtime.cleanup_stale()
    if n:
        live_task_ids = _report_runtime.task_ids()
        with _report_dedup_lock:
            stale_keys = [k for k, tid in _report_dedup_index.items()
                          if tid not in live_task_ids]
            for k in stale_keys:
                _report_dedup_index.pop(k, None)
        logger.debug('[Paper:Report] Cleaned %d stale task(s)', n)

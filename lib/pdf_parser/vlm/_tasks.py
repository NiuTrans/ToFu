"""Bounded owner-fair task lifecycle for VLM PDF transcription.

All shared mutable state for background VLM parse jobs lives HERE and
nowhere else:

    _vlm_tasks — the task registry dict (task_id → status dict)
    _vlm_lock  — guards _vlm_tasks
    _TASK_TTL  — expiry window for completed tasks

The owner-fair lane bounds retained source PDFs and resident coordinator
threads. Terminal results have both TTL and count caps; queued/running tasks
are never evicted. Cancellation removes queued work immediately or propagates
through page rendering and every dispatcher retry.

These are re-exported by reference from the package ``__init__`` so the
whole process shares exactly one registry — a divergent copy would lose
in-flight parse jobs.
"""

import threading
import time as _time

from lib.agent_core.fair_work_lane import (
    FairWorkLaneQueueFull,
    OwnerFairWorkLane,
)
from lib.error_envelope import from_exception, make_envelope
from lib.identity import require_user_id
from lib.ids import short_id
from lib.llm_errors import AbortedError
from lib.log import get_logger
from lib.pdf_parser.vlm._parse import vlm_parse_pdf
from lib.pdf_parser.vlm._policy import (
    vlm_queue_capacity,
    vlm_task_timeout_seconds,
    vlm_task_workers,
    vlm_worker_idle_seconds,
)

logger = get_logger(__name__)


# ── Async task management ─────────────────────────────

_vlm_tasks: dict[str, dict] = {}
_vlm_lock = threading.Lock()
_TASK_TTL = 1800  # 30 min
_VLM_TASK_WORKERS = vlm_task_workers()
_VLM_QUEUE_CAPACITY = vlm_queue_capacity()
_RESULT_CAPACITY = max(
    8, min(64, (_VLM_TASK_WORKERS + _VLM_QUEUE_CAPACITY) * 4))
_TERMINAL_STATUSES = frozenset({'done', 'error'})
_vlm_lane = OwnerFairWorkLane(
    max_workers=_VLM_TASK_WORKERS,
    queue_capacity=_VLM_QUEUE_CAPACITY,
    idle_seconds=vlm_worker_idle_seconds(),
    thread_name_prefix='tofu-pdf-vlm',
    metric_pool='pdf-vlm',
)


class VlmTaskQueueFull(RuntimeError):
    """The finite process VLM backlog has no remaining admission slot."""


def start_vlm_task(pdf_bytes: bytes, filename: str = 'document.pdf',
                   model: str | None = None, *, user_id: int) -> str:
    """Admit a background VLM parse. Returns *task_id* for polling."""
    owner_user_id = require_user_id(user_id, context='VLM parse task')
    task_id = short_id(n=12)
    abort_event = threading.Event()
    _cleanup_old_tasks()

    with _vlm_lock:
        _vlm_tasks[task_id] = {
            # ``processing`` is retained while queued for frontend backward
            # compatibility; ``progress`` makes admission state explicit.
            'status': 'processing', 'progress': 'queued',
            'result': None, 'error': None,
            'filename': filename, 'created': _time.time(),
            'user_id': owner_user_id,
            '_abort_event': abort_event,
            '_input_bytes': len(pdf_bytes),
        }

    def _run():
        deadline_at = _time.monotonic() + vlm_task_timeout_seconds()

        def _abort_check() -> bool:
            return abort_event.is_set() or _time.monotonic() >= deadline_at

        try:
            with _vlm_lock:
                current = _vlm_tasks.get(task_id)
                if current:
                    current['progress'] = '0/?'
                    current['started'] = _time.time()

            def _prog(done, total):
                with _vlm_lock:
                    t = _vlm_tasks.get(task_id)
                    if t:
                        t['progress'] = f'{done}/{total}'
            md = vlm_parse_pdf(
                pdf_bytes,
                model=model,
                progress_cb=_prog,
                abort_check=_abort_check,
            )
            if _abort_check():
                raise AbortedError('VLM PDF task stopped before publication')
            with _vlm_lock:
                t = _vlm_tasks.get(task_id)
                if t:
                    t['status'] = 'done'
                    t['result'] = md
                    t['finished'] = _time.time()
        except Exception as exc:
            public_error: Exception = exc
            if (isinstance(exc, AbortedError)
                    and not abort_event.is_set()
                    and _time.monotonic() >= deadline_at):
                public_error = TimeoutError(
                    'VLM PDF task exceeded its bounded execution deadline')
            if isinstance(public_error, AbortedError):
                logger.info('VLM task %s cancelled', task_id)
            else:
                logger.error(
                    'VLM task %s failed: %s', task_id, public_error,
                    exc_info=True)
            with _vlm_lock:
                t = _vlm_tasks.get(task_id)
                if t:
                    t['status'] = 'error'
                    t['error'] = from_exception(
                        public_error,
                        context='vlm-pdf-parse',
                        source='pdf-parser',
                    )
                    t['finished'] = _time.time()
        finally:
            _cleanup_old_tasks()

    def _isolated_run() -> None:
        import contextvars
        contextvars.Context().run(_run)

    try:
        _vlm_lane.submit_task(task_id, owner_user_id, _isolated_run)
    except FairWorkLaneQueueFull as exc:
        with _vlm_lock:
            _vlm_tasks.pop(task_id, None)
        raise VlmTaskQueueFull(
            'VLM PDF queue is full; retry after an active parse finishes') from exc
    except Exception:
        with _vlm_lock:
            _vlm_tasks.pop(task_id, None)
        raise
    return task_id


def get_vlm_task(task_id: str, *, user_id: int) -> dict | None:
    """Return task status dict, or None if not found."""
    owner_user_id = require_user_id(user_id, context='VLM task lookup')
    _cleanup_old_tasks()
    with _vlm_lock:
        t = _vlm_tasks.get(task_id)
        if not t or int(t.get('user_id') or 0) != owner_user_id:
            return None
        return {
            key: value for key, value in t.items()
            if not key.startswith('_')
        }


def find_vlm_tasks_by_filename(filename: str, *, user_id: int) -> list[dict]:
    """Find all active VLM tasks matching *filename*.

    Returns a list of ``{taskId, status, progress, filename, created, error}``
    dicts, most-recent first. ``error`` is a typed envelope when the task
    failed. Useful for reconnecting after a page
    refresh when the frontend lost the task_id.
    """
    owner_user_id = require_user_id(user_id, context='VLM task search')
    _cleanup_old_tasks()
    with _vlm_lock:
        matches = []
        for tid, t in _vlm_tasks.items():
            if (int(t.get('user_id') or 0) == owner_user_id
                    and t['filename'] == filename):
                matches.append({
                    'taskId': tid,
                    'status': t['status'],
                    'progress': t['progress'],
                    'filename': t['filename'],
                    'created': t['created'],
                    'error': t.get('error'),
                })
        matches.sort(key=lambda x: x['created'], reverse=True)
        return matches


def cancel_vlm_task(task_id: str, *, user_id: int) -> bool | None:
    """Cancel one owned queued/running parse without crossing owner scope."""
    owner_user_id = require_user_id(user_id, context='VLM task cancel')
    with _vlm_lock:
        task = _vlm_tasks.get(task_id)
        if not task or int(task.get('user_id') or 0) != owner_user_id:
            return None
        if task.get('status') in _TERMINAL_STATUSES:
            return False
        abort_event = task.get('_abort_event')
        if isinstance(abort_event, threading.Event):
            abort_event.set()
    if _vlm_lane.cancel_task(task_id):
        with _vlm_lock:
            task = _vlm_tasks.get(task_id)
            if task and task.get('status') not in _TERMINAL_STATUSES:
                task['status'] = 'error'
                task['error'] = make_envelope(
                    'aborted',
                    detail='VLM PDF task cancelled before execution',
                    context='vlm-pdf-parse',
                    source='pdf-parser',
                )
                task['finished'] = _time.time()
    return True


def vlm_task_snapshot() -> dict[str, int | float | bool]:
    """Return low-cardinality registry and execution-capacity evidence."""
    lane = _vlm_lane.snapshot()
    with _vlm_lock:
        active = sum(
            task.get('status') not in _TERMINAL_STATUSES
            for task in _vlm_tasks.values())
        retained_input_bytes = sum(
            int(task.get('_input_bytes') or 0)
            for task in _vlm_tasks.values()
            if task.get('status') not in _TERMINAL_STATUSES)
        terminal = len(_vlm_tasks) - active
    return {
        **lane,
        'registryActive': active,
        'registryTerminal': terminal,
        'retainedInputBytes': retained_input_bytes,
        'resultCapacity': _RESULT_CAPACITY,
    }


def _cleanup_old_tasks() -> int:
    """Evict expired/excess terminal results; never touch live tasks."""
    now = _time.time()
    with _vlm_lock:
        terminal = [
            (task_id, task)
            for task_id, task in _vlm_tasks.items()
            if task.get('status') in _TERMINAL_STATUSES
        ]
        remove_ids = {
            task_id for task_id, task in terminal
            if now - float(task.get('finished') or task.get('created') or 0)
            > _TASK_TTL
        }
        retained_terminal = sorted(
            (
                (float(task.get('finished') or task.get('created') or 0),
                 task_id)
                for task_id, task in terminal
                if task_id not in remove_ids
            ),
            reverse=True,
        )
        remove_ids.update(
            task_id
            for _finished, task_id in retained_terminal[_RESULT_CAPACITY:]
        )
        for task_id in remove_ids:
            _vlm_tasks.pop(task_id, None)
    return len(remove_ids)

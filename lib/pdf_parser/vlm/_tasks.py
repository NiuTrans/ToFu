"""lib/pdf_parser/vlm/_tasks.py — Async VLM parse task registry.

All shared mutable state for background VLM parse jobs lives HERE and
nowhere else:

    _vlm_tasks — the task registry dict (task_id → status dict)
    _vlm_lock  — guards _vlm_tasks
    _TASK_TTL  — expiry window for completed/stale tasks

These are re-exported by reference from the package ``__init__`` so the
whole process shares exactly one registry — a divergent copy would lose
in-flight parse jobs.
"""

import threading
import time as _time

from lib.ids import short_id
from lib.log import get_logger
from lib.pdf_parser.vlm._parse import vlm_parse_pdf

logger = get_logger(__name__)


# ── Async task management ─────────────────────────────

_vlm_tasks: dict[str, dict] = {}
_vlm_lock = threading.Lock()
_TASK_TTL = 1800  # 30 min


def start_vlm_task(pdf_bytes: bytes, filename: str = 'document.pdf',
                   model: str | None = None) -> str:
    """Launch a background VLM parse. Returns *task_id* for polling."""
    task_id = short_id(n=12)

    with _vlm_lock:
        _vlm_tasks[task_id] = {
            'status': 'processing', 'progress': '0/?',
            'result': None, 'error': None,
            'filename': filename, 'created': _time.time(),
        }

    def _run():
        try:
            def _prog(done, total):
                with _vlm_lock:
                    t = _vlm_tasks.get(task_id)
                    if t:
                        t['progress'] = f'{done}/{total}'
            md = vlm_parse_pdf(pdf_bytes, model=model, progress_cb=_prog)
            with _vlm_lock:
                t = _vlm_tasks.get(task_id)
                if t:
                    t['status'] = 'done'
                    t['result'] = md
        except Exception as exc:
            logger.error('VLM task %s failed: %s', task_id, exc, exc_info=True)
            with _vlm_lock:
                t = _vlm_tasks.get(task_id)
                if t:
                    t['status'] = 'error'
                    t['error'] = str(exc)
        finally:
            _cleanup_old_tasks()

    threading.Thread(target=_run, daemon=True, name=f'vlm-{task_id}').start()
    return task_id


def get_vlm_task(task_id: str) -> dict | None:
    """Return task status dict, or None if not found."""
    with _vlm_lock:
        t = _vlm_tasks.get(task_id)
        return dict(t) if t else None


def find_vlm_tasks_by_filename(filename: str) -> list[dict]:
    """Find all active VLM tasks matching *filename*.

    Returns a list of ``{taskId, status, progress, filename, created}``
    dicts, most-recent first.  Useful for reconnecting after a page
    refresh when the frontend lost the task_id.
    """
    with _vlm_lock:
        matches = []
        for tid, t in _vlm_tasks.items():
            if t['filename'] == filename:
                matches.append({
                    'taskId': tid,
                    'status': t['status'],
                    'progress': t['progress'],
                    'filename': t['filename'],
                    'created': t['created'],
                })
        matches.sort(key=lambda x: x['created'], reverse=True)
        return matches


def _cleanup_old_tasks():
    now = _time.time()
    with _vlm_lock:
        expired = [k for k, v in _vlm_tasks.items()
                   if now - v['created'] > _TASK_TTL]
        for k in expired:
            del _vlm_tasks[k]

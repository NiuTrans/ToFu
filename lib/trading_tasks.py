"""lib/trading_tasks.py — Background task manager for long-running trading operations.

Supports:
  • Submit a task → get task_id immediately
  • Task runs in a background thread (survives page refresh / navigation)
  • Incremental output chunks stored in memory for live polling
  • Final result persisted to DB for cross-session recovery
  • Automatic cleanup of stale tasks
"""

import json
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta

from lib.log import get_logger

logger = get_logger(__name__)

# ── In-memory task store ──
# { task_id: TaskState }
_tasks = OrderedDict()
_lock = threading.Lock()

# Limits
MAX_TASKS = 50          # Max concurrent tasks in memory
TASK_TTL = 3600 * 6     # Keep completed tasks for 6 hours


class TaskState:
    """In-memory representation of a running/completed task."""
    __slots__ = ('task_id', 'task_type', 'status', 'params',
                 'chunks', 'result', 'thinking', 'error',
                 'created_at', 'updated_at', 'thread',
                 '_cancel_flag')

    def __init__(self, task_id, task_type, params=None):
        self.task_id = task_id
        self.task_type = task_type
        self.status = 'running'       # running | done | error | cancelled
        self.params = params or {}
        self.chunks = []              # [{type:'thinking'|'content'|'phase', text:'...'}]
        self.result = ''              # Full accumulated content
        self.thinking = ''            # Full accumulated thinking
        self.error = ''
        self.created_at = time.time()
        self.updated_at = time.time()
        self.thread = None
        self._cancel_flag = False

    @property
    def cancelled(self):
        return self._cancel_flag

    def cancel(self):
        self._cancel_flag = True

    def add_chunk(self, chunk_type, text):
        """Add an output chunk (thread-safe via GIL for list.append)."""
        self.chunks.append({'type': chunk_type, 'text': text})
        if chunk_type == 'content':
            self.result += text
        elif chunk_type == 'thinking':
            self.thinking += text
        self.updated_at = time.time()

    def finish(self, error=None):
        """Mark task as done or error."""
        if error:
            self.status = 'error'
            self.error = str(error)
            self.chunks.append({'type': 'error', 'text': str(error)})
        else:
            self.status = 'done'
        self.chunks.append({'type': 'done', 'text': ''})
        self.updated_at = time.time()
        # Persist to DB
        _persist_task(self)

    def poll(self, cursor=0):
        """Get chunks since cursor. Returns (new_chunks, next_cursor, status)."""
        new_chunks = self.chunks[cursor:]
        return new_chunks, len(self.chunks), self.status

    def to_dict(self, include_chunks=False):
        d = {
            'task_id': self.task_id,
            'task_type': self.task_type,
            'status': self.status,
            'error': self.error,
            'chunk_count': len(self.chunks),
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }
        if include_chunks:
            d['chunks'] = self.chunks
            d['result'] = self.result
            d['thinking'] = self.thinking
        return d


def submit_task(task_type, run_fn, params=None):
    """Submit a background task.

    Args:
        task_type: 'decision' | 'autopilot' | 'intel_backtest'
        run_fn: callable(task: TaskState) — the work function.
                Should call task.add_chunk() and task.finish().
        params: optional dict of parameters (for logging/persistence)

    Returns:
        task_id (str)
    """
    task_id = f"{task_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    task = TaskState(task_id, task_type, params)

    with _lock:
        # Cleanup old tasks if we have too many
        _cleanup_stale()
        _tasks[task_id] = task

    def _wrapper():
        try:
            run_fn(task)
            if task.status == 'running':
                task.finish()
        except Exception as e:
            logger.error('Fund task %s failed: %s', task_id, e, exc_info=True)
            task.finish(error=str(e))

    t = threading.Thread(target=_wrapper, daemon=True, name=f'trading-task-{task_id}')
    task.thread = t
    t.start()
    return task_id


def get_task(task_id):
    """Get task by ID. Checks memory first, then DB."""
    with _lock:
        task = _tasks.get(task_id)
    if task:
        return task
    # Try loading from DB (completed task from a previous session)
    return _load_from_db(task_id)


def poll_task(task_id, cursor=0):
    """Poll for new chunks since cursor.

    Returns:
        dict with keys: chunks, cursor, status, task_id
        or None if task not found
    """
    task = get_task(task_id)
    if not task:
        return None
    new_chunks, next_cursor, status = task.poll(cursor)
    return {
        'task_id': task_id,
        'chunks': new_chunks,
        'cursor': next_cursor,
        'status': status,
        'error': task.error if task.error else None,
    }


def cancel_task(task_id):
    """Request cancellation of a running task."""
    task = get_task(task_id)
    if task and task.status == 'running':
        task.cancel()
        task.status = 'cancelled'
        task.chunks.append({'type': 'done', 'text': ''})
        _persist_task(task)
        return True
    return False


def list_active_tasks(task_type=None):
    """List all active (running) tasks, optionally filtered by type."""
    with _lock:
        tasks = list(_tasks.values())
    result = []
    for t in tasks:
        if task_type and t.task_type != task_type:
            continue
        result.append(t.to_dict())
    return result


# ── DB persistence ──

def _persist_task(task):
    """Save completed/errored task to DB for cross-session recovery."""
    try:
        from lib.database import DOMAIN_TRADING, get_thread_db
        db = get_thread_db(DOMAIN_TRADING)
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        db.execute('''
            INSERT OR REPLACE INTO trading_bg_tasks
            (task_id, task_type, status, params_json, result_json, thinking, error, created_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id, task.task_type, task.status,
            json.dumps(task.params, ensure_ascii=False),
            task.result, task.thinking, task.error,
            datetime.fromtimestamp(task.created_at).strftime('%Y-%m-%d %H:%M:%S'),
            now,
        ))
        db.commit()
    except Exception as e:
        logger.error('DB persist error: %s', e, exc_info=True)


def _load_from_db(task_id):
    """Load a completed task from DB (returns a TaskState with all chunks as one big chunk)."""
    try:
        from lib.database import DOMAIN_TRADING, get_thread_db
        db = get_thread_db(DOMAIN_TRADING)
        row = db.execute('SELECT * FROM trading_bg_tasks WHERE task_id=?', (task_id,)).fetchone()
        if not row:
            return None
        row = dict(row)
        task = TaskState(row['task_id'], row['task_type'])
        task.status = row['status']
        task.result = row.get('result_json', '') or ''
        task.thinking = row.get('thinking', '') or ''
        task.error = row.get('error', '')
        # Reconstruct minimal chunks for the poller
        if task.thinking:
            task.chunks.append({'type': 'thinking', 'text': task.thinking})
        if task.result:
            task.chunks.append({'type': 'content', 'text': task.result})
        if task.error:
            task.chunks.append({'type': 'error', 'text': task.error})
        task.chunks.append({'type': 'done', 'text': ''})
        return task
    except Exception as e:
        logger.error('DB load error: %s', e, exc_info=True)
        return None


def _cleanup_stale():
    """Remove tasks older than TTL from memory."""
    now = time.time()
    stale = [tid for tid, t in _tasks.items()
             if t.status != 'running' and (now - t.updated_at) > TASK_TTL]
    for tid in stale:
        del _tasks[tid]
    # If still over limit, remove oldest completed
    while len(_tasks) > MAX_TASKS:
        # Build list of removable IDs first to avoid RuntimeError from
        # modifying the dict during iteration.
        removable = [tid for tid, t in _tasks.items() if t.status != 'running']
        if not removable:
            break  # all running, can't remove
        del _tasks[removable[0]]


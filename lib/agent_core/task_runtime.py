"""Unified background task runtime.

Single source of truth for all server-side async tasks: chat orchestration,
paper report generation, translation, trading simulator, etc.

Replaces five near-identical implementations:
  - lib/tasks_pkg/manager.py (chat tasks)
  - routes/paper.py (_report_tasks, _translate_tasks)
  - routes/translate.py (_translate_tasks)
  - routes/trading_simulator.py (_tasks)

Each module instantiates one TaskRuntime per task kind, then uses:
  - runtime.create(...)              — register a new task
  - runtime.spawn(task_id, fn, *a)   — start the worker
  - runtime.append_event(id, event)  — emit progress (auto-pushes via WS)
  - runtime.finish(id, result=, error=) — terminal state
  - runtime.poll(id, cursor)         — cursor-based event replay
  - runtime.abort(id)                — request graceful stop
  - runtime.cleanup_stale()          — TTL-based purge

Standard task dict shape:
    {
        'id':           str,        # unique task ID
        'kind':         str,        # 'paper-report', 'translate', etc.
        'status':       str,        # 'pending'|'running'|'done'|'error'|'aborted'
        'events':       list[dict], # append-only, each gets a 'seq'
        'events_lock':  Lock,
        'abort_event':  threading.Event,
        'result':       Any,
        'error':        dict | None, # error envelope
        'created_at':   float,
        'finished_at':  float | None,
        'meta':         dict,        # caller-supplied custom fields
    }
"""

import asyncio
import threading
import time
from typing import Any, Callable, Optional

from lib.ids import short_id
from lib.log import get_logger

logger = get_logger(__name__)


def _make_envelope(error, *, context: str, source: str) -> Optional[dict]:
    """Normalize error to an envelope dict (or None)."""
    if error is None:
        return None
    if isinstance(error, dict):
        return error
    if isinstance(error, BaseException):
        from lib.error_envelope import from_exception as _err_from_exc
        return _err_from_exc(error, context=context, source=source)
    if isinstance(error, str):
        from lib.error_envelope import make_envelope as _make_env
        return _make_env('generic', detail=error, context=context,
                         source=source, raw=error)
    return {'kind': 'generic', 'detail': str(error), 'source': source}


class TaskRuntime:
    """Per-kind task registry with unified lifecycle, polling, and push.

    Thread-safe. Designed to be created once per task kind at module import:

        from lib.agent_core.task_runtime import TaskRuntime
        runtime = TaskRuntime('paper-report', ttl=3600, push_channel='paper')

    Then in routes:

        task = runtime.create(meta={'paper_hash': h, 'lang': 'zh'})
        runtime.spawn(task['id'], _run_report, task)
        return jsonify({'task_id': task['id']})
    """

    def __init__(self, kind: str, *, ttl: int = 3600,
                 push_channel: Optional[str] = None,
                 error_source: str = ''):
        """
        Args:
            kind: Task kind identifier (e.g. 'chat', 'paper-report').
            ttl: Seconds to retain finished tasks for late pollers.
            push_channel: WebSocket push channel name. If set, all events
                are also pushed via lib.agent_core.push.push_event(channel, task_id, event).
                If None, defaults to ``kind``.
            error_source: Module identifier for error envelopes.
        """
        self.kind = kind
        self.ttl = ttl
        self.push_channel = push_channel if push_channel is not None else kind
        self.error_source = error_source or f'task_runtime.{kind}'
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()
        # Strong references to in-flight asyncio worker tasks. The event loop
        # keeps only a WEAK reference to a bare ensure_future()/create_task()
        # result, so without this a worker Task could be GC'd mid-flight and
        # silently never run. Each entry self-evicts via add_done_callback.
        self._bg_tasks: set = set()

    # ── Task lifecycle ─────────────────────────────────────────

    def create(self, *, task_id: str = '', meta: Optional[dict] = None) -> dict:
        """Create and register a new task. Returns the task dict."""
        if not task_id:
            task_id = short_id(n=12)
        task = {
            'id': task_id,
            'kind': self.kind,
            'status': 'pending',
            'events': [],
            'events_lock': threading.Lock(),
            'abort_event': threading.Event(),
            'result': None,
            'error': None,
            'created_at': time.time(),
            'finished_at': None,
            'meta': meta or {},
        }
        with self._lock:
            self._tasks[task_id] = task
        logger.debug('[TaskRuntime:%s] created task %s', self.kind, task_id[:8])
        return task

    def get(self, task_id: str) -> Optional[dict]:
        """Get a task by ID. Returns None if not found."""
        with self._lock:
            return self._tasks.get(task_id)

    def list_running(self) -> list[dict]:
        """Return all currently-running tasks (snapshot)."""
        with self._lock:
            return [t for t in self._tasks.values()
                    if t['status'] in ('pending', 'running')]

    def append_event(self, task_id: str, event: dict,
                     *, before_push: Optional[Callable[[int], None]] = None) -> Optional[int]:
        """Append an event to the task. Auto-assigns 'seq'.

        Also pushes to the WebSocket channel (non-blocking, thread-safe).
        Returns the seq number, or None if task not found.

        ``before_push``: optional callback ``fn(seq)`` invoked AFTER the event
        is appended to ``task['events']`` (and its seq assigned) but BEFORE the
        frame is pushed to the client. This enforces **durable-before-visible**
        ordering: a caller that persists the event to a durable log (chat's
        ``append_persistent_event``) passes it here so the log is never behind
        what the client has already received — a cold reconnect can then
        reconstruct the COMPLETE stream. Best-effort: a callback exception is
        logged but never blocks the push (a DB blip must not stall the stream).

        Tolerant of legacy task dicts inserted directly into ``_tasks``
        (e.g. older test code) that may not have all the standard fields.
        """
        task = self.get(task_id)
        if not task:
            return None
        with task['events_lock']:
            event['seq'] = len(task['events'])
            task['events'].append(event)
            seq = event['seq']
        # Auto-transition pending → running on first event. Skip silently
        # for legacy dicts that have no 'status' key.
        if task.get('status') == 'pending':
            with self._lock:
                if task.get('status') == 'pending':
                    task['status'] = 'running'
        # ★ Durable-before-visible: commit the persistent row BEFORE the push,
        #   so task_events is never behind the bytes the client holds.
        if before_push is not None:
            try:
                before_push(seq)
            except Exception as e:
                logger.debug('[TaskRuntime:%s] before_push failed task=%s: %s',
                             self.kind, task_id[:8], e)
        if self.push_channel:
            try:
                from lib.agent_core.push import push_event
                push_event(self.push_channel, task_id, event)
            except Exception as e:
                logger.debug('[TaskRuntime:%s] push_event failed task=%s: %s',
                             self.kind, task_id[:8], e)
        return seq

    def finish(self, task_id: str, *, result: Any = None,
               error: Any = None, error_context: str = '') -> bool:
        """Mark a task as terminal (done | error | aborted).

        Always emits a final event with type='done' or type='error' so
        pollers/WebSocket subscribers see a guaranteed terminal frame.
        Returns True if the task was found and updated.
        """
        task = self.get(task_id)
        if not task:
            return False
        envelope = _make_envelope(error, context=error_context or self.kind,
                                  source=self.error_source)
        with self._lock:
            if task['status'] in ('done', 'error', 'aborted'):
                return False
            if task['abort_event'].is_set() and envelope is None:
                task['status'] = 'aborted'
            elif envelope:
                task['status'] = 'error'
            else:
                task['status'] = 'done'
            task['result'] = result
            task['error'] = envelope
            task['finished_at'] = time.time()
            final_status = task['status']

        terminal_event = {
            'type': 'done' if final_status == 'done' else (
                'aborted' if final_status == 'aborted' else 'error'),
            'status': final_status,
        }
        if envelope:
            terminal_event['error'] = envelope
        if result is not None and final_status == 'done':
            terminal_event['result'] = result
        self.append_event(task_id, terminal_event)
        logger.debug('[TaskRuntime:%s] task %s finished: %s',
                     self.kind, task_id[:8], final_status)
        return True

    def abort(self, task_id: str) -> bool:
        """Signal a task to abort. Workers must check task['abort_event'].
        Returns True if task exists and was running."""
        task = self.get(task_id)
        if not task:
            return False
        # Hold _lock so the status check + abort_event.set() is atomic w.r.t.
        # finish() (which reads abort_event.is_set() under the same lock to
        # decide done-vs-aborted). Without this an abort racing a finish could
        # be lost, marking a cancelled task 'done'.
        with self._lock:
            if task['status'] in ('done', 'error', 'aborted'):
                return False
            task['abort_event'].set()
        logger.info('[TaskRuntime:%s] abort requested for task %s',
                    self.kind, task_id[:8])
        return True

    # ── Polling ────────────────────────────────────────────────

    def poll(self, task_id: str, cursor: int = 0) -> dict:
        """Cursor-based event replay. Returns events since cursor + status.

        Response shape (matches the legacy implementations):
            {
                'ok': True,
                'events': [...new events...],
                'next_cursor': N,
                'status': 'pending'|'running'|'done'|'error'|'aborted',
                'done': bool,
                'result': ... (when done),
                'error': ... (when error),
            }

        If the task doesn't exist, returns {'ok': False, 'error': 'not_found'}.
        """
        task = self.get(task_id)
        if not task:
            return {'ok': False, 'error': 'not_found',
                    'events': [], 'next_cursor': cursor, 'done': True}

        with task['events_lock']:
            new_events = task['events'][cursor:]
            new_cursor = len(task['events'])

        terminal = task['status'] in ('done', 'error', 'aborted')
        resp = {
            'ok': True,
            'events': new_events,
            'next_cursor': new_cursor,
            'status': task['status'],
            'done': terminal,
        }
        if terminal:
            if task['error']:
                resp['error'] = task['error']
            elif task['result'] is not None:
                resp['result'] = task['result']
        return resp

    # ── Spawning ───────────────────────────────────────────────

    def spawn(self, task_id: str, fn: Callable, *args, **kwargs) -> None:
        """Spawn a worker function for the task.

        Inside an asyncio event loop: runs via asyncio.to_thread (tracked
        as an asyncio task, cancellable, awaitable).
        Outside: falls back to a daemon thread.

        The worker function receives whatever args are passed. It is the
        worker's responsibility to call runtime.append_event(...) and
        runtime.finish(...) appropriately.
        """
        def _wrapper():
            try:
                fn(*args, **kwargs)
            except Exception as e:
                logger.error('[TaskRuntime:%s] worker for task %s crashed: %s',
                             self.kind, task_id[:8], e, exc_info=True)
                self.finish(task_id, error=e,
                            error_context=f'{self.kind}:worker_crash')
            finally:
                # Workers run on the shared asyncio.to_thread executor pool;
                # those threads are long-lived and never die, so a thread-local
                # DB connection acquired during the task would be pinned forever
                # and exhaust the connection semaphore under load. Return it to
                # the pool now that this unit of work is done.
                try:
                    from lib.agent_core.store import get_conversation_store
                    get_conversation_store().release_connection()
                except Exception as _ctd_err:
                    logger.debug('[TaskRuntime:%s] release_connection failed task=%s: %s',
                                 self.kind, task_id[:8], _ctd_err)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as _e_audit:
            logger.debug('[task_runtime] spawn caught %s: %s', type(_e_audit).__name__, _e_audit)
            loop = None

        if loop and loop.is_running():
            async def _async_wrapper():
                await asyncio.to_thread(_wrapper)
            bg = asyncio.ensure_future(_async_wrapper())
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        else:
            threading.Thread(
                target=_wrapper,
                name=f'{self.kind}-{task_id[:8]}',
                daemon=True,
            ).start()

    # ── TTL cleanup ────────────────────────────────────────────

    def cleanup_stale(self, max_age: Optional[float] = None) -> int:
        """Remove finished tasks older than TTL. Returns count removed.

        ``max_age`` overrides ``self.ttl`` for this sweep only — pass a small
        value (e.g. 0) under memory pressure to evict every terminal task
        immediately, instead of waiting out the retention window. The steady
        cleanup tick passes nothing and keeps the normal TTL.
        """
        ttl = self.ttl if max_age is None else max_age
        now = time.time()
        expired = []
        with self._lock:
            for tid, task in list(self._tasks.items()):
                if task['status'] in ('done', 'error', 'aborted'):
                    finished = task.get('finished_at') or task.get('created_at', 0)
                    if now - finished > ttl:
                        expired.append(tid)
                        del self._tasks[tid]
        if expired:
            logger.debug('[TaskRuntime:%s] cleaned %d stale tasks',
                         self.kind, len(expired))
        return len(expired)

    # ── Stats ──────────────────────────────────────────────────

    @property
    def task_count(self) -> int:
        with self._lock:
            return len(self._tasks)

    def stats(self) -> dict:
        """Return aggregate stats for monitoring."""
        with self._lock:
            counts = {'pending': 0, 'running': 0, 'done': 0,
                      'error': 0, 'aborted': 0}
            for t in self._tasks.values():
                counts[t['status']] = counts.get(t['status'], 0) + 1
        return {'kind': self.kind, 'total': sum(counts.values()), **counts}

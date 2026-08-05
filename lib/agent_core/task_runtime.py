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
        'artifact_quality': dict|None,  # PRODUCT-quality axis, orthogonal to status
        'events':       list[dict], # append-only, each gets a 'seq'
        'events_lock':  Lock,
        'abort_event':  threading.Event,
        'result':       Any,
        'error':        dict | None, # error envelope
        'created_at':   float,      # true start — surfaced by poll()
        'updated_at':   float,      # last proof of life — surfaced by poll()
        'finished_at':  float | None,
        'meta':         dict,        # caller-supplied custom fields
    }

★ TWO INDEPENDENT AXES — do not conflate them:

  * ``status`` is the **lifecycle** axis: pending → running → terminal. Its
    membership is closed and load-bearing (every ``status in (…)`` terminal
    check in this file depends on it), so a new *quality* concern must never
    be added to it.
  * ``artifact_quality`` is the **product** axis: did the job deliver a GOOD
    artifact? A pipeline can complete its lifecycle cleanly (``status='done'``)
    while shipping an artifact produced by a sick pipeline — a research pass
    whose structural gate wiped every idea, a video whose narration silently
    degraded to silent, a report assembled with missing sections. Reporting
    those as plain 'done' is what made the R3 total-wipe bug invisible.

The field is ``artifact_quality`` and NOT the shorter ``quality`` because
``quality`` is already taken on a task dict: motion-video stores its render
preset there (``lib/motion_video/runtime.py`` — the string 'draft' /
'standard' / 'high', also a manifest field). Reusing the name made
``finish()`` do ``'standard'.get('degraded')`` and blew up three existing
tests. Two different meanings of the word 'quality' must not share a key.

``artifact_quality`` is tri-state on purpose:

  * ``None``  — this task kind does not assess quality (chat, translate…).
    NOT the same as "clean"; nobody looked.
  * ``{'degraded': False, 'reason': ''}`` — assessed and healthy.
  * ``{'degraded': True,  'reason': str}`` — valid artifact, sick pipeline.

Workers opt in by passing ``degraded=`` to :meth:`TaskRuntime.finish`. New
quality dimensions get a new KEY inside ``artifact_quality`` — never a new
``status`` member and never another top-level task field.
"""

import asyncio
import threading
import time
from typing import Any, Callable, Optional

from lib.ids import short_id
from lib.log import get_logger

logger = get_logger(__name__)


def _make_envelope(error, *, context: str, source: str) -> Optional[dict]:
    """Normalize error to an envelope dict (or None).

    Every shape becomes a COMPLETE envelope (``kind`` + string ``message``)
    because the frontend's ``isErrorEnvelope`` requires both — an incomplete
    dict (e.g. ``{'kind': 'worker_lost', 'detail': …}``) used to fall through
    to the renderer's unknown-shape branch and display as 'Unknown error'
    plus a JSON blob.
    """
    if error is None:
        return None
    if isinstance(error, dict):
        if isinstance(error.get('kind'), str) and isinstance(error.get('message'), str):
            return error  # complete envelope — pass through verbatim
        from lib.error_envelope import make_envelope as _make_env
        return _make_env(error.get('kind') or 'generic',
                         detail=str(error.get('detail') or '')[:300],
                         context=context,
                         source=error.get('source') or source,
                         raw=str(error)[:300])
    if isinstance(error, BaseException):
        from lib.error_envelope import from_exception as _err_from_exc
        return _err_from_exc(error, context=context, source=source)
    if isinstance(error, str):
        # A string naming a REGISTERED kind (e.g. finish(error='worker_lost')
        # — the documented stall-reap contract) builds that kind's envelope;
        # anything else is a raw reason string shown verbatim under 'generic'.
        from lib.error_envelope import KINDS as _KINDS, make_envelope as _make_env
        if error in _KINDS:
            return _make_env(error, context=context, source=source)
        return _make_env('generic', detail=error, context=context,
                         source=source, raw=error)
    from lib.error_envelope import make_envelope as _make_env
    return _make_env('generic', detail=str(error)[:300], context=context,
                     source=source, raw=str(error)[:300])


def _epoch_ms(seconds) -> Optional[int]:
    """Convert an internal epoch-SECONDS timestamp to wire epoch-MILLISECONDS.

    The unit boundary is deliberate and load-bearing. Internally every task
    clock is ``time.time()`` (float seconds); on the wire this project's
    established contract is **epoch milliseconds** under camelCase names
    (``createdAt`` — see ``lib/chat_dispatch.py`` and
    ``routes/chat_poll_abort.py``), because that is what JS ``Date.now()``
    speaks and what ``_seedStreamTimerStart`` consumes.

    Feeding a SECONDS value into that frontend seam is not a visible failure:
    the min-guard happily accepts it (a seconds epoch is ~1000x smaller than
    ``Date.now()``) and the UI then renders an elapsed of ~50 years. Keeping
    the snake_case seconds field and the camelCase millisecond field under
    DIFFERENT names is what makes that mistake impossible to make silently.

    Returns None for a missing/unset clock so the field is emitted as null
    rather than a bogus 0 (epoch 1970).
    """
    if seconds is None:
        return None
    try:
        return int(float(seconds) * 1000)
    except (TypeError, ValueError) as e:
        logger.debug('[task_runtime] non-numeric timestamp %r: %s', seconds, e)
        return None


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
                 error_source: str = '',
                 stall_timeout: float = 0):
        """
        Args:
            kind: Task kind identifier (e.g. 'chat', 'paper-report').
            ttl: Seconds to retain finished tasks for late pollers.
            push_channel: WebSocket push channel name. If set, all events
                are also pushed via lib.agent_core.push.push_event(channel, task_id, event).
                If None, defaults to ``kind``.
            error_source: Module identifier for error envelopes.
            stall_timeout: Read-side stall reaping (docs/PAPER_MEDIA_UX_DESIGN.md
                §3.2). When > 0, poll() declares a pending/running task whose
                last event is older than this many seconds ``worker_lost``.
                0 (default) disables reaping — only enable for runtimes whose
                workers heartbeat every long phase, or slow-but-legit phases
                (long tool calls) would be false-killed.
        """
        self.kind = kind
        self.ttl = ttl
        self.stall_timeout = float(stall_timeout or 0)
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
        _now = time.time()
        task = {
            'id': task_id,
            'kind': self.kind,
            'status': 'pending',
            # Product-quality axis (see module docstring). None = unassessed;
            # only a worker that passes degraded= to finish() populates it.
            'artifact_quality': None,
            'events': [],
            'events_lock': threading.Lock(),
            'abort_event': threading.Event(),
            'result': None,
            'error': None,
            'created_at': _now,
            # Set at creation (not only in append_event) so the liveness clock
            # is well-defined for a task that has not emitted anything yet.
            'updated_at': _now,
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
        # The stall-reap clock: every event is proof of life.
        task['updated_at'] = time.time()
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
               error: Any = None, error_context: str = '',
               degraded: Optional[bool] = None,
               degraded_reason: str = '') -> bool:
        """Mark a task as terminal (done | error | aborted).

        Always emits a final event with type='done' or type='error' so
        pollers/WebSocket subscribers see a guaranteed terminal frame.
        Returns True if the task was found and updated.

        ``degraded`` is the PRODUCT-quality axis and is deliberately
        orthogonal to ``status`` (module docstring). Pass it when the job
        delivered a valid artifact from a pipeline that did not work properly;
        ``status`` stays 'done' so every terminal check keeps its meaning and
        the frontend reads one extra field. Leaving it None means "this kind
        does not assess quality" — which is NOT the same as "clean".
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
            if degraded is not None:
                task['artifact_quality'] = {
                    'degraded': bool(degraded),
                    'reason': str(degraded_reason or ''),
                }
            task['finished_at'] = time.time()
            final_status = task['status']
            # .get(): legacy task dicts inserted straight into _tasks (older
            # test code, chat's own shape) predate this key.
            quality = task.get('artifact_quality')

        terminal_event = {
            'type': 'done' if final_status == 'done' else (
                'aborted' if final_status == 'aborted' else 'error'),
            'status': final_status,
        }
        if envelope:
            terminal_event['error'] = envelope
        if quality:
            # Ride the guaranteed terminal frame so a live SSE/WS subscriber
            # learns the verdict without a follow-up GET.
            terminal_event['artifact_quality'] = quality
        if result is not None and final_status == 'done':
            terminal_event['result'] = result
        self.append_event(task_id, terminal_event)
        logger.debug('[TaskRuntime:%s] task %s finished: %s%s',
                     self.kind, task_id[:8], final_status,
                     ' (DEGRADED)' if (quality or {}).get('degraded') else '')
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

    # ── Stall reaping (read-side, opt-in via stall_timeout) ────

    def reap_if_stalled(self, task: dict) -> bool:
        """Declare a silent pending/running task ``worker_lost`` (P-UX1).

        A task whose worker crashed (kill -9, process restart, thread death
        without finish) sits at ``running`` forever and every poller spins
        with it. There is no write-side reaper thread by design — the check
        runs on the poll path instead (a task nobody watches needs no
        verdict; self-healing, zero常驻 cost). The clock is ``updated_at``,
        touched by every append_event — workers that wrap their long phases
        in a heartbeat (lib/production/heartbeat.py) are never false-killed.

        Returns True when this call reaped the task.
        """
        if not self.stall_timeout:
            return False
        if not task or task.get('status') not in ('pending', 'running'):
            return False
        last = task.get('updated_at') or task.get('created_at') or 0
        if time.time() - last <= self.stall_timeout:
            return False
        task_id = task.get('id') or task.get('task_id') or '?'
        logger.warning('[TaskRuntime:%s] task %s stalled (no events for %.0fs '
                       '> %.0fs) — declaring worker_lost',
                       self.kind, str(task_id)[:8],
                       time.time() - last, self.stall_timeout)
        return self.finish(
            task_id,
            error={'kind': 'worker_lost',
                   'detail': 'no progress events for '
                             f'{self.stall_timeout:.0f}s — the worker '
                             'process is presumed dead; safe to retry',
                   'source': self.error_source},
            error_context=f'{self.kind}:stall')

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
                'createdAt': int,   # true job start, epoch MILLISECONDS
                'updatedAt': int,   # last proof of life, epoch MILLISECONDS
                'result': ... (when done),
                'error': ... (when error),
                'finishedAt': int (when terminal), epoch MILLISECONDS
            }

        ★ UNIT: the clock fields are epoch **milliseconds** under camelCase
        names, matching this project's existing task-start contract
        (``lib/chat_dispatch.py``, ``routes/chat_poll_abort.py``). The task
        dict's own ``created_at`` / ``updated_at`` stay float SECONDS; the
        camelCase/snake_case split is the unit marker. Never emit the raw
        seconds value on the wire — see :func:`_epoch_ms`.

        ``createdAt`` / ``updatedAt`` exist so a client that RE-ATTACHES to
        a running job (page refresh, tab switch, conversation switch) can
        continue the elapsed clock from the real start instead of restarting
        it at zero, and can render "last activity" from server truth. A client
        minting those locally re-mints them on every refresh, which not only
        shows a wrong elapsed but **washes an already-silent job into looking
        healthy** — the dangerous half. Mirrors the chat stream's
        server-authoritative rewind (``_seedStreamTimerStart``); clients MUST
        apply the same min-guard (only ever move the start EARLIER, ignore a
        future timestamp) so the display can never jump backward.

        If the task doesn't exist, returns {'ok': False, 'error': 'not_found'}
        with no clocks — a task that does not exist has no start time.
        """
        task = self.get(task_id)
        if not task:
            return {'ok': False, 'error': 'not_found',
                    'events': [], 'next_cursor': cursor, 'done': True}
        self.reap_if_stalled(task)

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
            'createdAt': _epoch_ms(task.get('created_at')),
            # Falls back to created_at so a task with no events yet still
            # reports a liveness clock — 'now' is never a safe default here.
            'updatedAt': _epoch_ms(task.get('updated_at')
                                   or task.get('created_at')),
        }
        # The making-model is part of the artifact's identity (paper podcast/
        # video panels badge it; the backend cache/dedup keys ride it) — a
        # live poll must be able to adopt it, not just a lookup re-attach.
        # Emitted only when the worker named one, so kinds that have no
        # model concept keep their frames unchanged.
        if task.get('model'):
            resp['model'] = task['model']
        if terminal:
            resp['finishedAt'] = _epoch_ms(task.get('finished_at'))
            # Product-quality axis, emitted only when the kind assessed it.
            # A poller that reads status alone still sees 'done' — by design.
            if task.get('artifact_quality'):
                resp['artifact_quality'] = task['artifact_quality']
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
            # INFO (was debug) + the evicted id prefixes: cleanup_stale is one
            # of only TWO registry-eviction paths (with discard_task), and a
            # task evaporating from the registry while alive was invisible
            # when this logged at debug (pt_a21cd6eb ③-1).
            logger.info('[TaskRuntime:%s] cleaned %d stale tasks: %s',
                        self.kind, len(expired),
                        [t[:8] for t in expired[:8]])
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

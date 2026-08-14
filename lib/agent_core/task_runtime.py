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
from lib.log import get_logger, req_id, set_req_id
from lib.task_replay import (
    TASK_REPLAY_EVENT_SEQUENCE_FIELD,
    TASK_REPLAY_EVENT_TYPE_FIELD,
    missing_replay_page,
    task_memory_replay_page,
    task_terminal_event_type,
)

logger = get_logger(__name__)


def _make_envelope(error, *, context: str, source: str) -> Optional[dict]:
    """Compatibility seam around the package-owned normalizer."""
    from lib.error_envelope import normalize_envelope
    return normalize_envelope(error, context=context, source=source)


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
                 max_tasks: int = 1024, max_events: int = 2048,
                 push_channel: Optional[str] = None,
                 error_source: str = '',
                 stall_timeout: float = 0):
        """
        Args:
            kind: Task kind identifier (e.g. 'chat', 'paper-report').
            ttl: Seconds to retain finished tasks for late pollers.
            max_tasks: Maximum retained task records per kind. Running tasks
                are never evicted; terminal records are removed oldest-first
                when a new task reaches this capacity.
            max_events: Maximum replay events retained per task. Sequence
                numbers remain absolute after old events are trimmed and poll
                responses mark a cursor reset when a client fell behind.
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
        self.max_tasks = max(1, int(max_tasks or 1))
        self.max_events = max(1, int(max_events or 1))
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
        request_id = req_id()
        task_meta = dict(meta or {})
        if request_id:
            task_meta.setdefault('requestId', request_id)
        task = {
            'id': task_id,
            'kind': self.kind,
            'status': 'pending',
            # Product-quality axis (see module docstring). None = unassessed;
            # only a worker that passes degraded= to finish() populates it.
            'artifact_quality': None,
            'events': [],
            '_eventBaseSeq': 0,
            '_eventNextSeq': 0,
            'events_lock': threading.Lock(),
            'abort_event': threading.Event(),
            'result': None,
            'error': None,
            'created_at': _now,
            # Set at creation (not only in append_event) so the liveness clock
            # is well-defined for a task that has not emitted anything yet.
            'updated_at': _now,
            'finished_at': None,
            'meta': task_meta,
            # Correlation is captured at ingress before work moves to a pool.
            # It is deliberately task data, not a Prometheus label.
            '_requestId': request_id,
        }
        capacity_evicted = []
        over_capacity = False
        with self._lock:
            if task_id not in self._tasks and len(self._tasks) >= self.max_tasks:
                terminal = sorted(
                    (item for item in self._tasks.values()
                     if item.get('status') in ('done', 'error', 'aborted')),
                    key=lambda item: float(item.get('finished_at')
                                           or item.get('created_at') or 0),
                )
                while terminal and len(self._tasks) >= self.max_tasks:
                    victim = terminal.pop(0)
                    victim_id = str(victim.get('id') or '')
                    if victim_id and self._tasks.pop(victim_id, None) is not None:
                        capacity_evicted.append(victim_id)
                over_capacity = len(self._tasks) >= self.max_tasks
            self._tasks[task_id] = task
        if capacity_evicted:
            logger.info(
                '[TaskRuntime:%s] capacity cleanup removed %d task(s): %s',
                self.kind, len(capacity_evicted),
                ','.join(item[:8] for item in capacity_evicted[:8]),
            )
            try:
                from lib.observability import record_registry_eviction
                record_registry_eviction(
                    self.kind, 'capacity', len(capacity_evicted))
            except Exception as exc:
                logger.debug('[TaskRuntime:%s] capacity metric skipped: %s',
                             self.kind, exc)
        if over_capacity:
            # Active work is authoritative and cannot be dropped to satisfy a
            # memory target. Surface this exceptional spill; the next terminal
            # create/TTL sweep returns the registry below its retention cap.
            logger.warning(
                '[TaskRuntime:%s] registry over capacity=%d; all retained '
                'tasks are active', self.kind, self.max_tasks)
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
        reconstruct the COMPLETE stream. Legacy non-terminal persistence stays
        best-effort; an authoritative turn/attempt frame is withheld on any
        callback failure because its persistent cursor is the protocol.

        Tolerant of legacy task dicts inserted directly into ``_tasks``
        (e.g. older test code) that may not have all the standard fields.
        """
        task = self.get(task_id)
        if not task:
            return None
        # Standard event envelope. These fields are additive and stable across
        # live WebSocket delivery, SSE and cursor replay.
        event.setdefault('taskId', task_id)
        request_id = task.get('_requestId') or (task.get('meta') or {}).get('requestId')
        if request_id:
            event.setdefault('requestId', request_id)
        # The stall-reap clock: every event is proof of life.
        task['updated_at'] = time.time()
        trimmed = 0
        with task['events_lock']:
            events = task.setdefault('events', [])
            try:
                hinted_next_seq = int(task.get('_eventNextSeq'))
            except (TypeError, ValueError, OverflowError) as exc:
                logger.debug('[TaskRuntime:%s] invalid next event sequence '
                             'task=%s: %s', self.kind, task_id[:8], exc)
                hinted_next_seq = -1
            # A retained event's wire ``seq`` is more authoritative than the
            # private hint. Reconcile on every append so a legacy/recovered
            # task with stale metadata cannot mint a duplicate or future id.
            if events:
                try:
                    next_seq = int(events[-1].get(
                        TASK_REPLAY_EVENT_SEQUENCE_FIELD)) + 1
                except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                    logger.debug('[TaskRuntime:%s] legacy event sequence '
                                 'fallback task=%s: %s',
                                 self.kind, task_id[:8], exc)
                    base_seq = task.get('_eventBaseSeq', 0)
                    try:
                        next_seq = max(0, int(base_seq)) + len(events)
                    except (TypeError, ValueError, OverflowError) as exc:
                        logger.debug('[TaskRuntime:%s] invalid event base '
                                     'task=%s: %s', self.kind, task_id[:8], exc)
                        next_seq = len(events)
            else:
                next_seq = hinted_next_seq if hinted_next_seq >= 0 else 0
            event[TASK_REPLAY_EVENT_SEQUENCE_FIELD] = next_seq
            events.append(event)
            seq = next_seq
            task['_eventNextSeq'] = seq + 1
            if len(events) > self.max_events:
                trimmed = len(events) - self.max_events
                del events[:trimmed]
            if events:
                try:
                    task['_eventBaseSeq'] = int(events[0].get(
                        TASK_REPLAY_EVENT_SEQUENCE_FIELD, seq + 1 - len(events)))
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.debug('[TaskRuntime:%s] invalid retained base '
                                 'sequence task=%s: %s',
                                 self.kind, task_id[:8], exc)
                    task['_eventBaseSeq'] = seq + 1 - len(events)
            else:
                task['_eventBaseSeq'] = seq + 1
        if trimmed:
            try:
                from lib.observability import record_task_event_eviction
                record_task_event_eviction(self.kind, trimmed)
            except Exception as exc:
                logger.debug('[TaskRuntime:%s] event eviction metric skipped: %s',
                             self.kind, exc)
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
                terminal = event.get(TASK_REPLAY_EVENT_TYPE_FIELD) in (
                    'done', 'error', 'aborted', 'interrupted')
                # v2 events have a stronger contract than the legacy task
                # channel: every frame is durable before visibility, not just
                # the terminal one.  A stale/superseded attempt therefore
                # withholds its late delta instead of leaking it to clients.
                authoritative = bool(event.get('attemptId'))
                if terminal or authoritative:
                    logger.error('[TaskRuntime:%s] authoritative persistence failed; '
                                 'withholding push task=%s seq=%s: %s',
                                 self.kind, task_id[:8], seq, e)
                    return seq
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
            TASK_REPLAY_EVENT_TYPE_FIELD: task_terminal_event_type(final_status),
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
                'format': 'tofu.task-replay/v1',
                'ok': True,
                'events': [...new events...],
                'next_cursor': N,
                'cursor': {'requested': N, 'next': N, 'reset': bool},
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
            return missing_replay_page(cursor).payload()
        self.reap_if_stalled(task)

        page = task_memory_replay_page(task, cursor)
        terminal = page.done
        try:
            from lib.observability import record_replay
            record_replay(
                'sse', self.kind, len(page.events), reset=page.cursor_reset)
        except Exception as exc:
            logger.debug('[TaskRuntime:%s] replay metric skipped task=%s: %s',
                         self.kind, task_id[:8], exc)

        extras = {
            'taskId': task_id,
            'createdAt': _epoch_ms(task.get('created_at')),
            # Falls back to created_at so a task with no events yet still
            # reports a liveness clock — 'now' is never a safe default here.
            'updatedAt': _epoch_ms(task.get('updated_at')
                                   or task.get('created_at')),
        }
        request_id = task.get('_requestId') or (task.get('meta') or {}).get('requestId')
        if request_id:
            extras['requestId'] = request_id
        # The making-model is part of the artifact's identity (paper podcast/
        # video panels badge it; the backend cache/dedup keys ride it) — a
        # live poll must be able to adopt it, not just a lookup re-attach.
        # Emitted only when the worker named one, so kinds that have no
        # model concept keep their frames unchanged.
        if task.get('model'):
            extras['model'] = task['model']
        if terminal:
            extras['finishedAt'] = _epoch_ms(task.get('finished_at'))
            # Product-quality axis, emitted only when the kind assessed it.
            # A poller that reads status alone still sees 'done' — by design.
            if task.get('artifact_quality'):
                extras['artifact_quality'] = task['artifact_quality']
            if task['error']:
                extras['error'] = task['error']
            elif task['result'] is not None:
                extras['result'] = task['result']
        return page.payload(extras)

    def retention_stats(self) -> dict[str, int | float]:
        """Return bounded registry/event occupancy without exposing task ids."""
        with self._lock:
            tasks = list(self._tasks.values())
        event_count = 0
        for task in tasks:
            lock = task.get('events_lock')
            if lock is None:
                event_count += len(task.get('events') or [])
                continue
            with lock:
                event_count += len(task.get('events') or [])
        return {
            'tasks': len(tasks),
            'max_tasks': self.max_tasks,
            'ttl_seconds': self.ttl,
            'events': event_count,
            'max_events_per_task': self.max_events,
            'over_capacity': max(0, len(tasks) - self.max_tasks),
        }

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
        task = self.get(task_id)
        worker_request_id = ''
        if task is not None:
            worker_request_id = str(
                task.get('_requestId')
                or (task.get('meta') or {}).get('requestId')
                or ''
            )
        queued_at = time.monotonic()

        def _wrapper():
            try:
                from lib.observability import record_task_queue_wait
                record_task_queue_wait(
                    self.kind, max(0.0, time.monotonic() - queued_at))
            except Exception as exc:
                logger.debug('[TaskRuntime:%s] queue-wait metric skipped: %s',
                             self.kind, exc)
            # The asyncio branch deliberately runs in a fresh Context so a
            # Quart request/app context (and its DB connection) cannot leak
            # into a long-lived worker. Re-seed only the inert correlation id
            # captured at task creation: provider, tool and exception logs then
            # remain joinable to the originating HTTP request without copying
            # any request-scoped resources into the pool thread.
            previous_request_id = req_id()
            if worker_request_id:
                set_req_id(worker_request_id)
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
                # Executor threads are reused. Restore the context that was
                # present before this unit of work (normally empty in the
                # deliberately fresh Context) so correlation never bleeds into
                # the next unrelated background task.
                set_req_id(previous_request_id)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as _e_audit:
            logger.debug('[task_runtime] spawn caught %s: %s', type(_e_audit).__name__, _e_audit)
            loop = None

        if loop and loop.is_running():
            # ``asyncio.to_thread`` copies the caller's ContextVars by
            # default. When spawn() is called by a Quart route that includes
            # the request/app context, the worker then mistakes itself for a
            # request and ``get_thread_db`` stores its connection on copied
            # ``g``. Request teardown cannot see that copied context and the
            # worker's close_thread_db() cannot see the g-bound connection:
            # an uncommitted SQLite write can therefore hold the global writer
            # lane forever. Run the worker inside a deliberately fresh context
            # while retaining to_thread's tracked executor lifecycle.
            import contextvars
            worker_context = contextvars.Context()

            async def _async_wrapper():
                await asyncio.to_thread(worker_context.run, _wrapper)
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
            try:
                from lib.observability import record_registry_eviction
                record_registry_eviction(self.kind, 'ttl', len(expired))
            except Exception as exc:
                logger.debug('[TaskRuntime:%s] eviction metric skipped: %s',
                             self.kind, exc)
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

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
        '_userId':      int,        # explicit owning principal
        'kind':         str,        # 'paper-report', 'translate', etc.
        'status':       str,        # pending/running or a shared terminal status
        'artifact_quality': dict|None,  # PRODUCT-quality axis, orthogonal to status
        'events':       list[dict], # append-only, each gets a 'seq'
        'events_lock':  Lock,
        'abort_event':  threading.Event,
        'result':       Any,
        'error':        dict | None, # error envelope
        'created_at':   float,      # task acceptance — surfaced by poll()
        'updated_at':   float,      # last proof of life — surfaced by poll()
        'finished_at':  float | None,
        'meta':         dict,        # caller-supplied custom fields
    }

TWO INDEPENDENT AXES — do not conflate them:

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
import json
import threading
import time
from typing import Any, Callable, Optional

from lib.agent_core.task_runtime_policy import (
    resolve_task_runtime_retention_budget,
)
from lib.agent_core.execution_session import (
    ExecutionPhase,
    ExecutionSession,
    execution_session_for_task,
)
from lib.ids import short_id
from lib.identity import PrincipalContext, require_user_id
from lib.log import bind_log_context, get_logger, req_id, set_req_id
from lib.task_replay import (
    TASK_REPLAY_EVENT_SEQUENCE_FIELD,
    TASK_REPLAY_EVENT_TYPE_FIELD,
    TASK_REPLAY_TERMINAL_STATUSES,
    missing_replay_page,
    task_memory_replay_page,
    task_terminal_event_type,
)

logger = get_logger(__name__)

try:
    import orjson as _orjson
except ImportError:  # pragma: no cover - minimal embedded installations
    _orjson = None


_TASK_RUNTIME_RETENTION_BUDGET = resolve_task_runtime_retention_budget()


_STALE_ATTEMPT_REJECTION_SUFFIX = (
    'event rejected: attempt is stale or no longer current'
)


_RUNTIME_OWNED_TASK_FIELDS = frozenset({
    'id',
    '_userId',
    '_principalContext',
    '_requestId',
    '_executionSession',
    '_executionTerminalizing',
    'kind',
    'status',
    'artifact_quality',
    'events',
    '_eventBaseSeq',
    '_eventNextSeq',
    '_eventRetainedBytes',
    '_eventRetainedSizes',
    '_eventOversizeWarned',
    'events_lock',
    'abort_event',
    'result',
    'error',
    'created_at',
    'updated_at',
    'finished_at',
    'meta',
})


def _make_envelope(error, *, context: str, source: str) -> Optional[dict]:
    """Lazy boundary around the package-owned error normalizer."""
    from lib.error_envelope import normalize_envelope
    return normalize_envelope(error, context=context, source=source)


def _epoch_ms(seconds) -> Optional[int]:
    """Convert an internal epoch-SECONDS timestamp to wire epoch-MILLISECONDS.

    The unit boundary is deliberate and load-bearing. Internally every task
    clock is ``time.time()`` (float seconds); on the wire this project's
    established contract is **epoch milliseconds** under camelCase names
    (``createdAt`` — see ``lib/chat_dispatch.py`` and
    ``routes/chat_poll_abort.py``), because that is what JS ``Date.now()``
    and the typed client clock adopters consume.

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


def _serialized_event_bytes(event: dict) -> int:
    """Return compact UTF-8 bytes retained by one replay event."""
    if _orjson is not None:
        try:
            return len(_orjson.dumps(event))
        except (TypeError, ValueError, OverflowError, RecursionError):
            # The stdlib accepts a few mappings that orjson deliberately
            # rejects. Try the canonical replay serializer before failing.
            pass
    return len(json.dumps(
        event, ensure_ascii=False, separators=(',', ':'),
    ).encode('utf-8'))


def _bounded_runtime_limit(value: Optional[int], policy_limit: int) -> int:
    """Let a composition lower, but never widen, a process policy limit."""
    if value is None:
        return policy_limit
    try:
        requested = int(value)
    except (TypeError, ValueError, OverflowError):
        return policy_limit
    return max(1, min(policy_limit, requested))


class TaskRuntime:
    """Per-kind task registry with unified lifecycle, polling, and push.

    Thread-safe. Designed to be created once per task kind at module import:

        from lib.agent_core.task_runtime import TaskRuntime
        runtime = TaskRuntime('paper-report', ttl=3600, push_channel='paper')

    Then in routes:

        task = runtime.create(user_id=user_id,
                              meta={'paper_hash': h, 'lang': 'zh'})
        runtime.spawn(task['id'], _run_report, task)
        return jsonify({'task_id': task['id']})
    """

    def __init__(self, kind: str, *, ttl: int = 3600,
                 max_tasks: Optional[int] = None,
                 max_events: Optional[int] = None,
                 max_event_buffer_bytes: Optional[int] = None,
                 max_event_bytes: Optional[int] = None,
                 push_channel: Optional[str] = None,
                 error_source: str = '',
                 stall_timeout: float = 0):
        """
        Args:
            kind: Task kind identifier (e.g. 'chat', 'paper-report').
            ttl: Seconds to retain finished tasks for late pollers.
            max_tasks: Maximum retained task records per kind. Running tasks
                and terminal records awaiting durable persistence are never
                evicted; safely persisted terminal records are removed
                oldest-first when a new task reaches this capacity.
            max_events: Maximum replay events retained per task. Sequence
                numbers remain absolute after old events are trimmed and poll
                responses mark a cursor reset when a client fell behind.
            max_event_buffer_bytes: Target serialized-byte ceiling for the
                retained replay tail. One individually valid event may occupy
                the window alone up to ``max_event_bytes``.
            max_event_bytes: Maximum serialized bytes for one event retained
                in memory. Larger/unencodable events still cross the existing
                persistence/live-delivery seams but reset the memory window.
            push_channel: WebSocket push channel name. If set, all events
                are also pushed via lib.agent_core.push.push_event(channel, task_id, event).
                If None, defaults to ``kind``.
            error_source: Module identifier for error envelopes.
            stall_timeout: Read-side stall reaping (docs/modules/ingest_media.md
                §3.2). When > 0, poll() declares a pending/running task whose
                last event is older than this many seconds ``worker_lost``.
                0 (default) disables reaping — only enable for runtimes whose
                workers heartbeat every long phase, or slow-but-legit phases
                (long tool calls) would be false-killed.
        """
        self.kind = kind
        self.ttl = ttl
        budget = _TASK_RUNTIME_RETENTION_BUDGET
        self.max_tasks = _bounded_runtime_limit(
            max_tasks, budget.task_capacity)
        self.max_events = _bounded_runtime_limit(
            max_events, budget.event_capacity)
        self.max_event_buffer_bytes = _bounded_runtime_limit(
            max_event_buffer_bytes, budget.replay_byte_capacity)
        self.max_event_bytes = _bounded_runtime_limit(
            max_event_bytes, budget.event_max_bytes)
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

    def create(
        self,
        *,
        principal: PrincipalContext | None = None,
        user_id: int | None = None,
        task_id: str = '',
        meta: Optional[dict] = None,
    ) -> dict:
        """Create a task with one normalized, durable principal snapshot.

        Existing service adapters may pass an explicit ``user_id`` while they
        migrate to ``PrincipalContext``; it is immediately normalized here,
        so no task can exist with only an ambient/default owner.
        """
        if principal is None:
            owner_user_id = require_user_id(
                user_id, context='TaskRuntime.create')
            principal = PrincipalContext.user(
                subject_id=f'user:{owner_user_id}',
                owner_user_id=owner_user_id,
            )
        else:
            if not isinstance(principal, PrincipalContext):
                raise TypeError('TaskRuntime.create principal must be PrincipalContext')
            owner_user_id = principal.require_owner(context='TaskRuntime.create')
            if user_id is not None and require_user_id(
                    user_id, context='TaskRuntime.create') != owner_user_id:
                raise ValueError('TaskRuntime.create principal/user_id mismatch')
        if not task_id:
            task_id = short_id(n=12)
        _now = time.time()
        request_id = req_id()
        task_meta = dict(meta or {})
        # Ownership is structural task data. Caller metadata cannot override
        # it, and append/push never infers it from ambient request state.
        task_meta['userId'] = owner_user_id
        if request_id:
            task_meta.setdefault('requestId', request_id)
        task = {
            'id': task_id,
            '_userId': owner_user_id,
            '_principalContext': principal.to_payload(),
            'kind': self.kind,
            'status': 'pending',
            # Product-quality axis (see module docstring). None = unassessed;
            # only a worker that passes degraded= to finish() populates it.
            'artifact_quality': None,
            'events': [],
            '_eventBaseSeq': 0,
            '_eventNextSeq': 0,
            '_eventRetainedBytes': 0,
            '_eventRetainedSizes': [],
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
            # Operational resource ownership is private and orthogonal to the
            # durable/user-visible task status. Routes bind leases to this
            # session; terminal paths settle it before publishing success.
            '_executionSession': ExecutionSession(
                execution_id=task_id,
                kind=self.kind,
                owner_user_id=owner_user_id,
                request_id=request_id,
            ),
            # Serializes the final cancellation decision with terminal
            # resource settlement without holding the runtime lock while
            # storage/provider cleanup callbacks execute.
            '_executionTerminalizing': False,
        }
        capacity_evicted = []
        over_capacity = False
        with self._lock:
            if task_id not in self._tasks and len(self._tasks) >= self.max_tasks:
                terminal = sorted(
                    (item for item in self._tasks.values()
                     if (item.get('status') in TASK_REPLAY_TERMINAL_STATUSES
                         and not item.get('_executionTerminalizing')
                         and not item.get('_finalize_started_at')
                         and not item.get('_terminalPersistencePending'))),
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
                'tasks are active or awaiting durable persistence',
                self.kind, self.max_tasks)
        logger.debug('[TaskRuntime:%s] created task %s', self.kind, task_id[:8])
        return task

    def get(self, task_id: str) -> Optional[dict]:
        """Internal task lookup; HTTP adapters must use :meth:`get_owned`."""
        with self._lock:
            return self._tasks.get(task_id)

    def snapshot(self) -> list[dict]:
        """Return a stable registry-membership snapshot for service policies.

        The returned task records remain the live records; callers that need to
        change them must use a lifecycle method, :meth:`update_fields`, or
        :meth:`update_matching`.  This keeps registry membership and locking
        private while allowing cross-task scheduling and liveness decisions.
        Request/HTTP code must use :meth:`snapshot_owned` instead.
        """
        with self._lock:
            return list(self._tasks.values())

    def update_matching(
        self,
        *,
        predicate: Callable[[dict], bool],
        updater: Callable[[dict], None],
    ) -> list[dict]:
        """Atomically update task records selected by a service policy.

        This is the explicit transaction boundary for manager-level policies
        that must inspect several records together (supersession, reaping,
        conversation-wide configuration).  Callbacks execute while the
        registry lock is held and therefore must not call back into this
        runtime.  HTTP adapters must use owner-scoped lifecycle methods rather
        than this fleet-wide service API.
        """
        matched: list[dict] = []
        with self._lock:
            for task in self._tasks.values():
                if predicate(task):
                    updater(task)
                    matched.append(task)
        return matched

    @staticmethod
    def _validate_custom_fields(fields: dict, remove_fields: tuple[str, ...]) -> None:
        """Reject writes to lifecycle fields owned exclusively by the runtime."""
        requested = set(fields) | set(remove_fields)
        forbidden = sorted(requested & _RUNTIME_OWNED_TASK_FIELDS)
        if forbidden:
            raise ValueError(
                'TaskRuntime custom-field mutation cannot write runtime-owned '
                f'fields: {", ".join(forbidden)}')

    def mark_running(self, task_id: str, *, fields: Optional[dict] = None) -> bool:
        """Atomically move a pending task to running and initialize custom state.

        Repeating the call for an already-running task is safe. Terminal tasks
        are immutable and return ``False``. Lifecycle fields remain owned by
        :class:`TaskRuntime`; capability-specific presentation state belongs in
        ``fields`` or the task's immutable creation metadata.
        """
        custom_fields = dict(fields or {})
        self._validate_custom_fields(custom_fields, ())
        with self._lock:
            task = self._tasks.get(task_id)
            if (task is None
                    or task.get('status') not in ('pending', 'running')
                    or task.get('_executionTerminalizing')):
                return False
            try:
                session = execution_session_for_task(task)
                if session.is_terminal:
                    return False
                session.mark_dispatch_started()
                if session.is_terminal:
                    return False
            except (RuntimeError, ValueError) as exc:
                logger.error(
                    '[TaskRuntime:%s] execution start invariant failed '
                    'task=%s: %s',
                    self.kind, task_id[:8], exc,
                )
                return False
            task['status'] = 'running'
            task.update(custom_fields)
            task['updated_at'] = time.time()
        return True

    def update_fields(
        self,
        task_id: str,
        *,
        fields: Optional[dict] = None,
        remove_fields: tuple[str, ...] = (),
        only_if_status: str | tuple[str, ...] | None = None,
    ) -> bool:
        """Atomically update capability-owned fields on one registered task.

        ``only_if_status`` closes progress-vs-terminal races: a late callback
        can update presentation state only while the task is still running.
        Core identity, event-log, and lifecycle fields are deliberately
        rejected; callers use ``mark_running`` / ``finish`` / ``abort`` for
        those transitions.
        """
        custom_fields = dict(fields or {})
        removals = tuple(str(field) for field in remove_fields)
        self._validate_custom_fields(custom_fields, removals)
        if only_if_status is None:
            allowed_statuses = None
        elif isinstance(only_if_status, str):
            allowed_statuses = (only_if_status,)
        else:
            allowed_statuses = tuple(only_if_status)
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return False
            if allowed_statuses is not None and task.get('status') not in allowed_statuses:
                return False
            task.update(custom_fields)
            for field in removals:
                task.pop(field, None)
            task['updated_at'] = time.time()
        return True

    def get_owned(self, task_id: str, *, user_id: int) -> Optional[dict]:
        """Return a task only when it belongs to the explicit principal."""
        if (isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id < 1):
            raise ValueError('TaskRuntime.get_owned requires a positive user_id')
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or int(task.get('_userId') or 0) != user_id:
                return None
            return task

    def snapshot_owned(self, *, user_id: int) -> list[dict]:
        """Return one stable owner-scoped registry snapshot."""
        if (isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id < 1):
            raise ValueError(
                'TaskRuntime.snapshot_owned requires a positive user_id')
        with self._lock:
            return [
                task for task in self._tasks.values()
                if int(task.get('_userId') or 0) == user_id
            ]

    def task_ids(self) -> frozenset[str]:
        """Return the registered IDs without exposing registry storage."""
        with self._lock:
            return frozenset(self._tasks)

    def task_statuses(self) -> dict[str, str]:
        """Return a stable ``task_id -> lifecycle status`` snapshot."""
        with self._lock:
            return {
                task_id: str(task.get('status') or '')
                for task_id, task in self._tasks.items()
            }

    def adopt(self, task: dict) -> bool:
        """Re-register a LIVE task dict that fell out of the registry.

        Root fix for the withholding-push flood (2026-08-21): a task whose
        worker is still emitting events but whose registry row vanished
        (false terminal-flip by a stall reaper followed by TTL/capacity
        eviction, or any direct-dict producer) made ``append_event`` return
        None forever; the chat manager's legacy fallback then minted
        ``seq = len(events)`` and every frame collided with the original
        run's durable rows ('Event sequence has a conflicting payload'), so
        every authoritative push was withheld and the client froze until
        refresh. Re-adopting the live dict returns it to the monotonic
        runtime path.

        Refuses (returns False, caller keeps its fallback):
          * terminal tasks — finished work must not resurrect as a phantom
            'running' row;
          * tombstoned tasks (``_discarded_at``) — a dict that
            ``discard_task`` deliberately unregistered (e.g. the autopilot
            VU carrier's designed retirement) must stay out;
          * foreign-kind dicts.

        The caller is responsible for seeding ``_eventNextSeq`` from the
        durable log BEFORE adopting when the task may already own durable
        rows; ``append_event``'s retained-event reconcile only looks at the
        in-memory list, which a partial run leaves diverged.

        Idempotent: if the id is already registered (a racing re-register),
        the registry copy wins and True is returned.
        """
        task_id = str((task or {}).get('id') or '')
        if not task_id:
            return False
        if task.get('status') in TASK_REPLAY_TERMINAL_STATUSES:
            return False
        if task.get('_discarded_at'):
            return False
        kind = task.get('kind')
        if kind not in (None, '', self.kind):
            return False
        owner_user_id = task.get('_userId')
        if (isinstance(owner_user_id, bool)
                or not isinstance(owner_user_id, int)
                or owner_user_id < 1):
            logger.error(
                '[TaskRuntime:%s] refused ownerless live-task adoption id=%s',
                self.kind, task_id[:8])
            return False
        raw_principal = task.get('_principalContext')
        try:
            if raw_principal is None:
                # Migration seam for live task dicts created before the
                # structured identity contract. Their explicit owner is
                # sufficient to construct a non-ambient principal once.
                principal = PrincipalContext.user(
                    subject_id=f'user:{owner_user_id}',
                    owner_user_id=owner_user_id,
                )
                task['_principalContext'] = principal.to_payload()
            else:
                if not isinstance(raw_principal, dict):
                    raise ValueError('principal snapshot must be an object')
                principal = PrincipalContext.from_payload(raw_principal)
                if principal.require_owner(
                        context='TaskRuntime.adopt') != owner_user_id:
                    raise ValueError('principal owner mismatch')
        except (PermissionError, TypeError, ValueError) as exc:
            logger.error(
                '[TaskRuntime:%s] refused invalid-principal adoption id=%s: %s',
                self.kind, task_id[:8], exc)
            return False
        # Fill the standard fields a bare/legacy dict may lack so the
        # runtime paths (append_event's events_lock, abort's abort_event)
        # never KeyError on the adopted entry.
        task.setdefault('kind', self.kind)
        task.setdefault('events', [])
        task.setdefault('_eventBaseSeq', 0)
        task.setdefault('_eventNextSeq', 0)
        # Adoption accepts legacy/external task dictionaries. Rebuild their
        # private accounting once instead of trusting caller-supplied totals.
        task['_eventRetainedBytes'] = None
        task['_eventRetainedSizes'] = None
        if 'events_lock' not in task:
            task['events_lock'] = threading.Lock()
        if 'abort_event' not in task:
            task['abort_event'] = threading.Event()
        task.setdefault('status', 'running')
        task.setdefault('result', None)
        task.setdefault('error', None)
        task.setdefault('artifact_quality', None)
        task.setdefault('_executionTerminalizing', False)
        _now = time.time()
        task.setdefault('created_at', _now)
        task.setdefault('updated_at', _now)
        task.setdefault('finished_at', None)
        task.setdefault('meta', {})
        with self._lock:
            if task_id in self._tasks:
                return True
            self._tasks[task_id] = task
        # WARNING, not info/debug: a live task needing re-adoption means an
        # eviction path (or a producer) dropped a running row — the flood
        # class this closes was invisible for hours at lower levels.
        logger.warning('[TaskRuntime:%s] re-adopted live task %s (was missing '
                       'from the registry while still emitting events)',
                       self.kind, task_id[:8])
        return True

    def list_running(self) -> list[dict]:
        """Return all currently-running tasks (snapshot)."""
        with self._lock:
            return [t for t in self._tasks.values()
                    if t['status'] in ('pending', 'running')]

    def _reconcile_event_retention(
        self,
        task: dict,
        events: list,
    ) -> tuple[list[int], int]:
        """Repair private byte accounting for a legacy/mutated task window."""
        raw_sizes = task.get('_eventRetainedSizes')
        raw_retained_bytes = task.get('_eventRetainedBytes')
        if (isinstance(raw_sizes, list)
                and len(raw_sizes) == len(events)
                and isinstance(raw_retained_bytes, int)
                and raw_retained_bytes >= 0):
            retained_bytes = raw_retained_bytes
        else:
            raw_sizes = []
            for retained_event in events:
                try:
                    retained_bytes_for_event = _serialized_event_bytes(
                        retained_event)
                except (TypeError, ValueError, OverflowError, RecursionError):
                    retained_bytes_for_event = self.max_event_bytes + 1
                raw_sizes.append(retained_bytes_for_event)
            retained_bytes = sum(raw_sizes)
            task['_eventRetainedSizes'] = raw_sizes
        task['_eventRetainedBytes'] = retained_bytes
        return raw_sizes, retained_bytes

    def append_event(self, task_id: str, event: dict,
                     *, before_push: Optional[Callable[[int], None]] = None,
                     deliver_push: bool = True) -> Optional[int]:
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

        ``deliver_push=False`` retains the event in the bounded task replay
        buffer but skips synchronous push listeners/bus publication.  The chat
        task manager uses this only while it is draining an upstream provider
        stream: browser/webhook observers must not delay model ingress, and
        the first post-ingress authoritative event converges delivery again.

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
        oversized_event_bytes = 0
        serialization_error = None
        with task['events_lock']:
            events = task.setdefault('events', [])
            retained_sizes, retained_bytes = self._reconcile_event_retention(
                task, events)
            try:
                hinted_next_seq = int(task.get('_eventNextSeq'))
            except (TypeError, ValueError, OverflowError, RecursionError) as exc:
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
            try:
                event_bytes = _serialized_event_bytes(event)
            except (TypeError, ValueError, OverflowError) as exc:
                # An unencodable object has no finite replay wire shape. Treat
                # it as oversized so the task never retains an unaccounted
                # object graph; the existing persistence/push seams still own
                # their normal typed failure and reconciliation behavior.
                event_bytes = self.max_event_bytes + 1
                serialization_error = exc
            events.append(event)
            retained_sizes.append(event_bytes)
            retained_bytes += event_bytes
            seq = next_seq
            task['_eventNextSeq'] = seq + 1
            if event_bytes > self.max_event_bytes:
                # A rolling replay window must remain one contiguous suffix.
                # Dropping only the newest event would make next_cursor move
                # backwards, so an individually oversized event resets the
                # entire reconstructible window at its next absolute seq.
                oversized_event_bytes = event_bytes
                trimmed = len(events)
                events.clear()
                retained_sizes.clear()
                retained_bytes = 0
            else:
                trimmed = max(0, len(events) - self.max_events)
                retained_after_trim = retained_bytes - sum(
                    retained_sizes[:trimmed])
                # Keep at least the newest valid event intact even when it is
                # larger than the ordinary tail target. Its separate finite
                # single-event ceiling remains the hard per-task bound.
                while (retained_after_trim > self.max_event_buffer_bytes
                       and trimmed < len(events) - 1):
                    retained_after_trim -= retained_sizes[trimmed]
                    trimmed += 1
                if trimmed:
                    del retained_sizes[:trimmed]
                    retained_bytes = retained_after_trim
                del events[:trimmed]
            task['_eventRetainedBytes'] = retained_bytes
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
            if oversized_event_bytes and not task.get('_eventOversizeWarned'):
                task['_eventOversizeWarned'] = True
                if serialization_error is not None:
                    logger.warning(
                        '[TaskRuntime:%s] unencodable event reset memory replay '
                        'task=%s seq=%s: %s', self.kind, task_id[:8], seq,
                        serialization_error)
                else:
                    logger.warning(
                        '[TaskRuntime:%s] event bytes=%d exceed max=%d; reset '
                        'memory replay task=%s seq=%s', self.kind,
                        oversized_event_bytes, self.max_event_bytes,
                        task_id[:8], seq)
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
        # Durable-before-visible: commit the persistent row BEFORE the push,
        #   so task_events is never behind the bytes the client holds.
        if before_push is not None:
            try:
                before_push(seq)
                if '_pushWithheldAt' in task:
                    # Persistence made it through the seam again — the wedge
                    # is over (new frames are no longer being withheld).
                    task.pop('_pushWithheldAt', None)
                    task.pop('_pushWithheldCount', None)
            except Exception as e:
                terminal = event.get(TASK_REPLAY_EVENT_TYPE_FIELD) in (
                    'done', 'error', 'aborted', 'interrupted')
                # v2 events have a stronger contract than the legacy task
                # channel: every frame is durable before visibility, not just
                # the terminal one.  A stale/superseded attempt therefore
                # withholds its late delta instead of leaking it to clients.
                authoritative = bool(event.get('attemptId'))
                if terminal or authoritative:
                    # If this is a known cooperative-abort fence (e.g. v2 stale
                    # attempt), _events.py already logged a single WARNING and
                    # flagged the abort. Log at DEBUG here to avoid flooding
                    # error.log with thousands of identical ERROR rows while
                    # the worker loop unwinds.
                    is_stale_fence = str(e).endswith(
                        _STALE_ATTEMPT_REJECTION_SUFFIX)
                    log_fn = logger.debug if is_stale_fence else logger.error
                    log_fn('[TaskRuntime:%s] authoritative persistence failed; '
                           'withholding push task=%s seq=%s: %s',
                           self.kind, task_id[:8], seq, e)
                    # Delivery-wedge marker (the 2026-08-19 msy4gswgss7tjd
                    #   case): the task keeps 'running' while EVERY
                    #   authoritative frame is withheld, so a task-alive probe
                    #   alone would call this an "explained silence" even
                    #   though no output can ever arrive. chat_poll ships this
                    #   so the frontend escalates it to an actionable verdict.
                    #   Cleared by the next frame whose persist succeeds.
                    task['_pushWithheldAt'] = time.time()
                    task['_pushWithheldCount'] = int(
                        task.get('_pushWithheldCount') or 0) + 1
                    return seq
                logger.debug('[TaskRuntime:%s] before_push failed task=%s: %s',
                             self.kind, task_id[:8], e)
        if self.push_channel and deliver_push:
            try:
                from lib.agent_core.push import push_event
                push_event(
                    self.push_channel,
                    task_id,
                    event,
                    user_id=int(task['_userId']),
                )
            except Exception as e:
                logger.debug('[TaskRuntime:%s] push_event failed task=%s: %s',
                             self.kind, task_id[:8], e)
        return seq

    def finish(self, task_id: str, *, result: Any = None,
               error: Any = None, error_context: str = '',
               degraded: Optional[bool] = None,
               degraded_reason: str = '',
               terminal_event_fields: Optional[dict] = None) -> bool:
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

        ``terminal_event_fields`` adds capability-specific correlation or
        presentation fields to the one authoritative terminal event. Runtime
        fields (type/status/result/error/quality) always win, so a caller
        cannot publish a terminal frame that disagrees with task state.
        """
        envelope = _make_envelope(error, context=error_context or self.kind,
                                  source=self.error_source)
        with self._lock:
            task = self._tasks.get(task_id)
            if (task is None
                    or task.get('status') in TASK_REPLAY_TERMINAL_STATUSES
                    or task.get('_executionTerminalizing')):
                return False
            task['_executionTerminalizing'] = True
            abort_requested = task['abort_event'].is_set()
        requested_outcome = (
            ExecutionPhase.CANCELLED
            if abort_requested and envelope is None
            else ExecutionPhase.FAILED if envelope
            else ExecutionPhase.COMPLETED
        )
        try:
            execution_receipt = execution_session_for_task(task).settle(
                requested_outcome,
                cause=(str((envelope or {}).get('kind') or '')
                       if envelope else ''),
            )
        except ValueError:
            # Adopted legacy/test carriers may predate the private session.
            execution_receipt = None
        except BaseException:
            with self._lock:
                if self._tasks.get(task_id) is task:
                    task['_executionTerminalizing'] = False
            raise
        settled_cancelled = bool(
            execution_receipt is not None
            and execution_receipt.outcome in {
                ExecutionPhase.CANCELLED,
                ExecutionPhase.TIMED_OUT,
            }
        )
        if (execution_receipt is not None
                and execution_receipt.outcome is ExecutionPhase.FAILED
                and envelope is None):
            envelope = _make_envelope(
                RuntimeError(
                    'execution failed before task terminal publication'),
                context=error_context or self.kind,
                source=self.error_source,
            )
        with self._lock:
            if (self._tasks.get(task_id) is not task
                    or task['status'] in TASK_REPLAY_TERMINAL_STATUSES):
                task['_executionTerminalizing'] = False
                return False
            if (abort_requested or settled_cancelled) and envelope is None:
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

        terminal_event = dict(terminal_event_fields or {})
        for runtime_field in (
                TASK_REPLAY_EVENT_TYPE_FIELD,
                TASK_REPLAY_EVENT_SEQUENCE_FIELD,
                'taskId', 'status', 'result', 'error', 'artifact_quality'):
            terminal_event.pop(runtime_field, None)
        terminal_event.update({
            TASK_REPLAY_EVENT_TYPE_FIELD: task_terminal_event_type(final_status),
            'status': final_status,
        })
        if envelope:
            terminal_event['error'] = envelope
        if quality:
            # Ride the guaranteed terminal frame so a live SSE/WS subscriber
            # learns the verdict without a follow-up GET.
            terminal_event['artifact_quality'] = quality
        if result is not None and final_status == 'done':
            terminal_event['result'] = result
        try:
            self.append_event(task_id, terminal_event)
        finally:
            with self._lock:
                if self._tasks.get(task_id) is task:
                    task['_executionTerminalizing'] = False
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
            if (task['status'] in TASK_REPLAY_TERMINAL_STATUSES
                    or task.get('_executionTerminalizing')):
                return False
            task['abort_event'].set()
        logger.info('[TaskRuntime:%s] abort requested for task %s',
                    self.kind, task_id[:8])
        return True

    def abort_owned(self, task_id: str, *, user_id: int) -> bool:
        """Atomically abort only a task owned by ``user_id``."""
        if (isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id < 1):
            raise ValueError('TaskRuntime.abort_owned requires a positive user_id')
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or int(task.get('_userId') or 0) != user_id:
                return False
            if (task['status'] in TASK_REPLAY_TERMINAL_STATUSES
                    or task.get('_executionTerminalizing')):
                return False
            task['abort_event'].set()
        logger.info('[TaskRuntime:%s] owner abort requested for task %s',
                    self.kind, task_id[:8])
        return True

    def remove_owned(self, task_id: str, *, user_id: int) -> bool:
        """Atomically remove only a task owned by ``user_id``.

        This is the administrative registry operation.  HTTP adapters must
        not mutate ``_tasks`` directly because doing so separates the
        ownership check from the destructive write.
        """
        if (isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id < 1):
            raise ValueError('TaskRuntime.remove_owned requires a positive user_id')
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None or int(task.get('_userId') or 0) != user_id:
                return False
            if (task.get('_executionTerminalizing')
                    or task.get('_finalize_started_at')
                    or task.get('_terminalPersistencePending')):
                return False
            try:
                execution_session = execution_session_for_task(task)
            except ValueError:
                execution_session = None
            if (execution_session is not None
                    and execution_session.dispatch_started
                    and not execution_session.is_terminal):
                # Removing registry authority while a worker is inside a
                # provider/tool call would either leak its session forever or
                # release admission/routes underneath live work. Request Stop
                # and let the ordinary terminal owner remove it later.
                task['abort_event'].set()
                return False
            self._tasks.pop(task_id, None)
        try:
            execution_session_for_task(task).settle(
                ExecutionPhase.CANCELLED, cause='task_removed')
        except ValueError:
            # Adopted legacy/test carriers may predate the private session.
            pass
        logger.info('[TaskRuntime:%s] owner removed task %s',
                    self.kind, task_id[:8])
        return True

    def discard(self, task_id: str) -> Optional[dict]:
        """Unregister one task for an internal lifecycle policy.

        Unlike :meth:`remove_owned`, this is not an HTTP authorization seam;
        it is used by the task manager after the caller has already established
        why a carrier or test-owned record must leave the registry.  Returning
        the removed record lets the manager tombstone it against re-adoption.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if (task is None
                    or task.get('_executionTerminalizing')
                    or task.get('_finalize_started_at')
                    or task.get('_terminalPersistencePending')):
                return None
            self._tasks.pop(task_id, None)
        if task is not None:
            try:
                execution_session_for_task(task).settle(
                    ExecutionPhase.CANCELLED, cause='task_discarded')
            except ValueError:
                pass
        return task

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
                'createdAt': int,   # task acceptance, epoch MILLISECONDS
                'updatedAt': int,   # last proof of life, epoch MILLISECONDS
                'result': ... (when done),
                'error': ... (when error),
                'finishedAt': int (when terminal), epoch MILLISECONDS
            }

        UNIT: the clock fields are epoch **milliseconds** under camelCase
        names, matching this project's existing task-clock contract
        (``lib/chat_dispatch.py``, ``routes/chat_poll_abort.py``). The task
        dict's own ``created_at`` / ``updated_at`` stay float SECONDS; the
        camelCase/snake_case split is the unit marker. Never emit the raw
        seconds value on the wire — see :func:`_epoch_ms`.

        ``createdAt`` / ``updatedAt`` exist so a client that RE-ATTACHES to
        a running job (page refresh, tab switch, conversation switch) can
        continue the elapsed clock from server acceptance instead of restarting
        it at zero, and can render "last activity" from server truth. A client
        minting those locally re-mints them on every refresh, which not only
        shows a wrong elapsed but **washes an already-silent job into looking
        healthy** — the dangerous half. Clients MUST preserve the shared
        server-authoritative clock rule (only ever move the start EARLIER and
        ignore a future timestamp) so the display can never jump backward.

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
        event_retained_bytes = 0
        for task in tasks:
            lock = task.get('events_lock')
            if lock is None:
                events = task.get('events') or []
                event_count += len(events)
                _, retained_bytes = self._reconcile_event_retention(
                    task, events)
                event_retained_bytes += retained_bytes
                continue
            with lock:
                events = task.get('events') or []
                event_count += len(events)
                _, retained_bytes = self._reconcile_event_retention(
                    task, events)
                event_retained_bytes += retained_bytes
        return {
            'tasks': len(tasks),
            'max_tasks': self.max_tasks,
            'ttl_seconds': self.ttl,
            'events': event_count,
            'event_retained_bytes': event_retained_bytes,
            'max_events_per_task': self.max_events,
            'event_buffer_byte_capacity_per_task': (
                self.max_event_buffer_bytes),
            'event_max_bytes': self.max_event_bytes,
            'event_retention_hard_capacity_per_task': max(
                self.max_event_buffer_bytes, self.max_event_bytes),
            'over_capacity': max(0, len(tasks) - self.max_tasks),
        }

    # ── Spawning ───────────────────────────────────────────────

    def _build_worker_callable(
        self,
        task_id: str,
        fn: Callable,
        args: tuple,
        kwargs: dict,
    ) -> Callable[[], None]:
        """Build the one context/error/lifecycle boundary for worker entry."""
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
                meta = (task.get('meta') or {}) if task is not None else {}
                with bind_log_context(
                        task_id=task_id,
                        conversation_id=(meta.get('conversationId')
                                         or meta.get('convId') or ''),
                        trace_id=worker_request_id,
                        user_id=meta.get('userId') or ''):
                    fn(*args, **kwargs)
            except Exception as e:
                logger.error('[TaskRuntime:%s] worker for task %s crashed: %s',
                             self.kind, task_id[:8], e, exc_info=True)
                self.finish(task_id, error=e,
                            error_context=f'{self.kind}:worker_crash')
            finally:
                # Executor threads are reused. Restore the context that was
                # present before this unit of work (normally empty in the
                # deliberately fresh Context) so correlation never bleeds into
                # the next unrelated background task.
                set_req_id(previous_request_id)

        return _wrapper

    def submit_worker(
        self,
        task_id: str,
        submitter: Callable[[str, int, Callable[[], None]], Any],
        fn: Callable,
        *args,
        **kwargs,
    ) -> Any:
        """Submit through an injected bounded scheduler.

        ``submitter`` receives ``(task_id, owner_user_id, worker_callable)``.
        This preserves the same request-context isolation, correlation, queue
        timing, and crash settlement as :meth:`spawn` while allowing a domain
        owner to enforce finite capacity and owner-fair scheduling. Admission
        exceptions propagate so that owner can map saturation explicitly.
        """
        task = self.get(task_id)
        if task is None:
            raise KeyError(f'unknown {self.kind} task: {task_id}')
        owner_user_id = int(task.get('_userId') or 0)
        if owner_user_id <= 0:
            raise ValueError(
                f'{self.kind} task {task_id} has no positive owner')
        worker = self._build_worker_callable(
            task_id, fn, tuple(args), dict(kwargs))

        def _isolated_worker() -> None:
            # A submitter may execute inline, create a thread from inside a
            # request, or reuse a long-lived pool thread. Make isolation a
            # TaskRuntime guarantee instead of relying on any scheduler's
            # ContextVar inheritance defaults.
            import contextvars
            contextvars.Context().run(worker)

        return submitter(task_id, owner_user_id, _isolated_worker)

    def spawn(self, task_id: str, fn: Callable, *args, **kwargs) -> None:
        """Spawn a worker function for the task.

        Inside an asyncio event loop: runs via asyncio.to_thread (tracked
        as an asyncio task, cancellable, awaitable).
        Outside: falls back to a daemon thread.

        The worker function receives whatever args are passed. It is the
        worker's responsibility to call runtime.append_event(...) and
        runtime.finish(...) appropriately.
        """
        _wrapper = self._build_worker_callable(
            task_id, fn, tuple(args), dict(kwargs))

        def _isolated_wrapper() -> None:
            import contextvars
            contextvars.Context().run(_wrapper)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as _e_audit:
            logger.debug('[task_runtime] spawn caught %s: %s', type(_e_audit).__name__, _e_audit)
            loop = None

        if loop and loop.is_running():
            # ``asyncio.to_thread`` copies the caller's ContextVars by default.
            # A background task must not inherit request-scoped authentication,
            # correlation, or framework context, so run it inside a deliberately
            # fresh context while retaining to_thread's tracked lifecycle.
            async def _async_wrapper():
                await asyncio.to_thread(_isolated_wrapper)
            bg = asyncio.ensure_future(_async_wrapper())
            self._bg_tasks.add(bg)
            bg.add_done_callback(self._bg_tasks.discard)
        else:
            threading.Thread(
                target=_isolated_wrapper,
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
                if task['status'] in TASK_REPLAY_TERMINAL_STATUSES:
                    if (task.get('_executionTerminalizing')
                            or task.get('_finalize_started_at')
                            or task.get('_terminalPersistencePending')):
                        continue
                    finished = task.get('finished_at') or task.get('created_at', 0)
                    if now - finished > ttl:
                        expired.append(tid)
                        del self._tasks[tid]
        if expired:
            # INFO (was debug) + the evicted id prefixes: cleanup_stale is one
            # of only TWO registry-eviction paths (with discard_task), and a
            # task evaporating from the registry while alive was invisible
            # when this logged at debug ( ③-1).
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

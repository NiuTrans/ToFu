"""lib/production/runtime.py — ProductionRuntime: the long-job runtime layer.

P6 extraction, driven by the P7 measurement (docs/PRODUCTION_PIPELINE_DESIGN.md
§9): writing a third capability showed the per-capability ``runtime.py`` is
**67% byte-identical after renaming** across motion-video / paper-podcast /
longform-report. This module is that shared 67%.

It is a thin layer OVER :class:`lib.task_runtime.TaskRuntime`, not a
replacement — TaskRuntime already owns the task registry, event log, push,
poll, spawn and terminal states. What every production capability had to
hand-roll on top of it, and what lives here now:

  * **dedup index** — "a second identical request joins the in-flight job
    instead of regenerating", including pruning entries whose task died;
  * **create + field-shape update** — ``runtime.create()`` then ``.update()``
    with the worker's expected fields;
  * **append + touch** — append an event and bump ``updated_at``;
  * **stale sweep** — TTL purge keyed on ``updated_at`` (not TaskRuntime's
    ``finished_at``), plus dedup-index pruning;
  * **id minting** — ``<prefix>_<uuid16>``.

Deliberately NOT here: the binary ``deliverable`` channel. The P7 measurement
found sample 3 (a markdown report) did not need it, so it is a video/podcast
commonality rather than a global one — abstracting it now would be exactly the
"wrong shape from too few samples" mistake the design note's risk table warns
about.
"""

from __future__ import annotations

import time
import threading
import uuid
import weakref
from typing import Any, Callable, Optional

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

__all__ = ['ProductionRuntime', 'production_retention_stats']


_instances_lock = threading.Lock()
_instances: weakref.WeakSet = weakref.WeakSet()


def production_retention_stats() -> list[dict]:
    """Return bounded dedup-index occupancy for every live capability.

    Only finite capability kinds and eviction reasons are exposed. Dedup keys
    and task ids are deliberately omitted because both are user-shaped and
    would create unbounded Prometheus label cardinality.
    """
    with _instances_lock:
        instances = list(_instances)
    return sorted(
        (runtime.dedup_stats() for runtime in instances),
        key=lambda row: row['kind'],
    )


class ProductionRuntime:
    """A :class:`TaskRuntime` plus the dedup / lifecycle helpers every
    "one sentence → finished product" capability needs.

    Args:
        kind: task kind, e.g. ``'motion-video'``. Also the ``?kind=`` filter
            value on ``/api/v1/tasks`` — always read back from ``.kind``,
            never re-typed as a literal at the call site.
        id_prefix: minted task ids are ``<id_prefix>_<uuid16>``.
        ttl / push_channel / error_source: passed through to TaskRuntime.
        log_label: human label used in this layer's log lines.
    """

    def __init__(self, kind: str, *, id_prefix: str, ttl: int = 3600,
                 push_channel: Optional[str] = None, error_source: str = '',
                 log_label: str = '', stall_timeout: float = 0,
                 max_tasks: int = 1024, max_events: int = 2048,
                 max_dedup_keys: Optional[int] = None):
        self.runtime = TaskRuntime(kind, ttl=ttl, push_channel=push_channel,
                                   error_source=error_source,
                                   stall_timeout=stall_timeout,
                                   max_tasks=max_tasks, max_events=max_events)
        self.id_prefix = id_prefix
        self.log_label = log_label or kind
        self._dedup: dict[tuple, str] = {}
        self.max_dedup_keys = max(
            1, int(max_dedup_keys if max_dedup_keys is not None else max_tasks))
        self._dedup_evictions = {
            'terminal': 0,
            'orphan': 0,
            'ttl': 0,
        }
        # Separate from TaskRuntime._lock: create_task() takes that registry
        # lock internally, so a claim cannot safely hold it across
        # index_get→create→register. This lock closes exactly that check/create
        # race without changing TaskRuntime's locking semantics.
        self._dedup_claim_lock = threading.RLock()
        with _instances_lock:
            _instances.add(self)

    # ── Pass-throughs (so callers need only one object) ───────

    @property
    def kind(self) -> str:
        return self.runtime.kind

    @property
    def ttl(self) -> int:
        return self.runtime.ttl

    @property
    def tasks(self) -> dict:
        """The live task registry dict (shared with TaskRuntime)."""
        return self.runtime._tasks       # type: ignore[attr-defined]

    @property
    def lock(self):
        """The registry lock (shared with TaskRuntime)."""
        return self.runtime._lock        # type: ignore[attr-defined]

    @property
    def dedup_index(self) -> dict:
        return self._dedup

    def get(self, task_id: str):
        return self.runtime.get(task_id)

    def poll(self, task_id: str, cursor: int = 0) -> dict:
        return self.runtime.poll(task_id, cursor)

    def abort(self, task_id: str) -> bool:
        return self.runtime.abort(task_id)

    def spawn(self, task_id: str, fn: Callable, *args, **kwargs) -> None:
        self.runtime.spawn(task_id, fn, *args, **kwargs)

    def finish(self, task_id: str, **kw) -> bool:
        return self.runtime.finish(task_id, **kw)

    # ── Id minting ────────────────────────────────────────────

    def new_task_id(self) -> str:
        return f'{self.id_prefix}_{uuid.uuid4().hex[:16]}'

    # ── Dedup index ───────────────────────────────────────────

    def index_get(self, key: tuple) -> Optional[str]:
        """Return a LIVE task_id for ``key``, pruning the entry if its task
        is gone or already terminal."""
        with self._dedup_claim_lock:
            tid = self._dedup.get(key)
            if not tid:
                return None
            with self.lock:
                task = self.tasks.get(tid)
                if task and task.get('status') in ('pending', 'running'):
                    return tid
            self._dedup.pop(key, None)
            reason = 'terminal' if task is not None else 'orphan'
            self._dedup_evictions[reason] += 1
            return None

    def index_register(self, key: tuple, task_id: str) -> None:
        with self._dedup_claim_lock:
            self._dedup[key] = task_id
            self._prune_orphaned_index_locked('orphan')

    def _prune_orphaned_index_locked(self, orphan_reason: str) -> int:
        """Remove keys that no longer protect live work.

        The caller holds ``_dedup_claim_lock``. Active keys are authoritative:
        if active work temporarily exceeds the configured capacity we expose
        that pressure instead of dropping dedup protection and launching a
        duplicate expensive job.
        """
        with self.lock:
            statuses = {
                task_id: task.get('status')
                for task_id, task in self.tasks.items()
            }
        removed = {'terminal': 0, orphan_reason: 0}
        for key, task_id in list(self._dedup.items()):
            status = statuses.get(task_id)
            if status in ('pending', 'running'):
                continue
            self._dedup.pop(key, None)
            reason = 'terminal' if task_id in statuses else orphan_reason
            removed[reason] = removed.get(reason, 0) + 1
        for reason, count in removed.items():
            if count:
                self._dedup_evictions[reason] += count
        return sum(removed.values())

    def dedup_stats(self) -> dict:
        """Return low-cardinality capacity and cleanup counters."""
        with self._dedup_claim_lock:
            size = len(self._dedup)
            evictions = dict(self._dedup_evictions)
        return {
            'kind': self.kind,
            'size': size,
            'capacity': self.max_dedup_keys,
            'over_capacity': max(0, size - self.max_dedup_keys),
            'evictions': evictions,
        }

    def claim_task(self, key: tuple, task_id: str, *,
                   meta: Optional[dict] = None,
                   fields: Optional[dict] = None) -> tuple[Optional[dict], Optional[str]]:
        """Atomically join-or-create a task for a dedup key.

        Returns ``(task, None)`` for the winner and ``(None, existing_id)`` for
        every concurrent loser. Keeping the check, registry create and index
        registration under one small dedicated lock prevents identical starts
        from launching parallel expensive jobs whose artifacts race each
        other.
        """
        with self._dedup_claim_lock:
            existing = self.index_get(key)
            if existing:
                return None, existing
            task = self.create_task(task_id, meta=meta, fields=fields)
            self.index_register(key, task_id)
            return task, None

    # ── Task creation + events ────────────────────────────────

    def create_task(self, task_id: str, *, meta: Optional[dict] = None,
                    fields: Optional[dict] = None) -> dict:
        """Create + register a pending task carrying the worker's field shape.

        ``meta`` is the TaskRuntime meta dict (surfaced by the generic task
        API); ``fields`` are the extra top-level keys this capability's worker
        reads. ``task_id`` / ``status`` / ``updated_at`` are always set.
        """
        task = self.runtime.create(task_id=task_id, meta=meta or {})
        task.update({
            'task_id': task_id,
            'status': 'pending',
            'result': None,
            'updated_at': time.time(),
        })
        if fields:
            task.update(fields)
        return task

    def append_event(self, task: dict, event: dict) -> Any:
        """Append one event (monotonic seq + WS push) and touch the task."""
        seq = self.runtime.append_event(task['task_id'], event)
        task['updated_at'] = time.time()
        return seq

    # ── Stale sweep ───────────────────────────────────────────

    def cleanup_stale(self) -> int:
        """Drop terminal tasks past TTL and prune orphaned dedup entries.

        Keyed on ``updated_at`` (when the job last did something), which is
        what the capabilities used — TaskRuntime's own sweep keys on
        ``finished_at`` and does not know about the dedup index.
        """
        now = time.time()
        with self.lock:
            stale = [tid for tid, t in self.tasks.items()
                     if t.get('status') in ('done', 'error', 'aborted')
                     and now - t.get('updated_at', now) > self.ttl]
            for tid in stale:
                self.tasks.pop(tid, None)
        with self._dedup_claim_lock:
            expired_ids = set(stale)
            ttl_keys = [key for key, task_id in self._dedup.items()
                        if task_id in expired_ids]
            for key in ttl_keys:
                self._dedup.pop(key, None)
            self._dedup_evictions['ttl'] += len(ttl_keys)
            self._prune_orphaned_index_locked('orphan')
        if stale:
            logger.info('[%s] cleaned %d stale task(s)', self.log_label,
                        len(stale))
        return len(stale)

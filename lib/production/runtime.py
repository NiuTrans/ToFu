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
import uuid
from typing import Any, Callable, Optional

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)

__all__ = ['ProductionRuntime']


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
                 log_label: str = '', stall_timeout: float = 0):
        self.runtime = TaskRuntime(kind, ttl=ttl, push_channel=push_channel,
                                   error_source=error_source,
                                   stall_timeout=stall_timeout)
        self.id_prefix = id_prefix
        self.log_label = log_label or kind
        self._dedup: dict[tuple, str] = {}

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
        tid = self._dedup.get(key)
        if not tid:
            return None
        with self.lock:
            t = self.tasks.get(tid)
            if t and t.get('status') in ('pending', 'running'):
                return tid
        self._dedup.pop(key, None)
        return None

    def index_register(self, key: tuple, task_id: str) -> None:
        self._dedup[key] = task_id

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
        for key, tid in list(self._dedup.items()):
            with self.lock:
                if tid not in self.tasks:
                    self._dedup.pop(key, None)
        if stale:
            logger.info('[%s] cleaned %d stale task(s)', self.log_label,
                        len(stale))
        return len(stale)

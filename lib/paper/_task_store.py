"""Shared index-backed task store for the paper runtimes.

The four paper runtimes (report / translate / qa / recommend) each wrap a
:class:`~lib.agent_core.task_runtime.TaskRuntime` with an identical companion
structure: a small index dict (``key → task_id``) plus a lock, and three
copy-pasted helpers — ``index_get`` / ``index_register`` / ``cleanup_stale`` —
that differed only in the key type (a ``(paper_hash, lang)`` tuple for the
deduped report/translate stores, a plain string for the per-request qa/recommend
stores) and the log label.

``IndexedTaskStore`` factors that skeleton out once. Each runtime module keeps
its own ``_new_*_task`` factory (they set different legacy task-dict fields) and
re-exports the store's ``runtime`` / ``index`` / ``index_lock`` under the
module-level names the package facade, ``routes/paper.py`` and the migration
tests already depend on — so object identity and the public surface are
unchanged.
"""

import threading
import time
from typing import Any, Hashable, Optional

from lib.log import get_logger
from lib.task_runtime import TaskRuntime

logger = get_logger(__name__)


class IndexedTaskStore:
    """A :class:`TaskRuntime` plus a ``key → task_id`` lookup index.

    Args:
        runtime: The backing task runtime.
        ttl: Seconds after which a finished task (and its index entry) is purged.
        log_label: Optional ``[Label]`` prefix for the cleanup debug log. When
            ``None`` cleanup stays silent (matches the original translate store).
    """

    def __init__(self, runtime: TaskRuntime, *, ttl: int,
                 log_label: Optional[str] = None):
        self.runtime = runtime
        self.ttl = ttl
        self.log_label = log_label
        self.index: dict[Hashable, str] = {}
        self.index_lock = threading.Lock()

    def index_get(self, key: Hashable) -> Optional[dict]:
        """Return the task registered under ``key``, or ``None``."""
        with self.index_lock:
            tid = self.index.get(key)
        if not tid:
            return None
        return self.runtime.get(tid)

    def index_register(self, key: Hashable, task_id: str) -> None:
        """Map ``key`` → ``task_id`` in the index."""
        with self.index_lock:
            self.index[key] = task_id

    def append_event(self, task: dict, event: Any) -> None:
        """Append an event to ``task``'s log (thread-safe; auto-pushes)."""
        self.runtime.append_event(task['task_id'], event)

    def cleanup_stale(self) -> int:
        """Drop finished tasks past TTL and prune their index entries.

        Snapshots the finished-and-expired ids BEFORE delegating to
        ``runtime.cleanup_stale()`` so the matching index keys can be removed.
        Returns the number of tasks purged.
        """
        finished_ids: set = set()
        with self.runtime._lock:
            for tid, t in self.runtime._tasks.items():
                if t['status'] in ('done', 'error', 'aborted') and t.get('finished_at'):
                    if time.time() - t['finished_at'] > self.ttl:
                        finished_ids.add(tid)
        n = self.runtime.cleanup_stale()
        if n:
            with self.index_lock:
                stale = [k for k, tid in self.index.items() if tid in finished_ids]
                for k in stale:
                    self.index.pop(k, None)
            if self.log_label:
                logger.debug('[%s] Cleaned %d stale task(s)', self.log_label, n)
        return n

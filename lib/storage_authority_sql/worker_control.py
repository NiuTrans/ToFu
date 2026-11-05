"""Commit-owned, content-free worker wakeups; no storage transaction or journal.

The SQL adapter publishes staged hints after COMMIT. Transport waits hold only
this condition, never a database connection. Queue and cancellation generations
are distinct so ordinary queue traffic never fans out to running handlers.
"""
from collections.abc import Callable
from dataclasses import dataclass, field
import threading
import time
import uuid

from lib.storage.errors import StorageError
from lib.storage.generated_worker_control import FORMAT


@dataclass
class WorkerCommitHints:
    queue_changed: bool = False
    cancelled_tasks: set[str] = field(default_factory=set)
    publisher: Callable[[bool, set[str]], None] | None = None

    def publish(self):
        if self.publisher is not None and (self.queue_changed or self.cancelled_tasks):
            self.publisher(self.queue_changed, self.cancelled_tasks)


class WorkerControl:
    def __init__(self):
        self._condition = threading.Condition()
        self._epoch = str(uuid.uuid4())
        self._queue = self._cancel = 0
        self._waiters = {}
        self._closed = False

    def _cursor(self):
        return dict(epoch=self._epoch, queue=self._queue, cancel=self._cancel)

    def publish(self, queue_changed, cancelled_tasks):
        with self._condition:
            if self._closed:
                return
            if max(self._queue, self._cancel) == (1 << 64) - 1:
                self._epoch, self._queue, self._cancel = str(uuid.uuid4()), 0, 0
            self._queue += bool(queue_changed)
            self._cancel += bool(cancelled_tasks)
            for tasks in self._waiters.values():
                tasks.update(cancelled_tasks)
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def watch(self, payload, deadline_at):
        if not isinstance(payload, dict) or set(payload) != {'cursor'}:
            raise StorageError('database_protocol_error', 'Invalid worker watch request')
        cursor = payload['cursor']
        if cursor is not None and (
            not isinstance(cursor, dict) or set(cursor) != {'epoch', 'queue', 'cancel'}
            or not isinstance(cursor['epoch'], str)
            or any(type(cursor[k]) is not int or not 0 <= cursor[k] < 1 << 64
                   for k in ('queue', 'cancel'))
        ):
            raise StorageError('database_protocol_error', 'Invalid worker watch cursor')
        with self._condition:
            current = self._cursor()
            cancelled = None
            waiter = object()
            if cursor == current and not self._closed:
                tasks = set()
                self._waiters[waiter] = tasks
                try:
                    self._condition.wait_for(
                        lambda: self._closed or cursor != self._cursor(),
                        max(0, deadline_at - time.monotonic()))
                    cancelled = sorted(tasks)
                finally:
                    del self._waiters[waiter]
            elif cursor is not None and cursor['epoch'] == current['epoch'] and cursor['cancel'] == current['cancel']:
                cancelled = []
            if self._closed:
                raise StorageError('database_unavailable', 'Worker control closed')
            return dict(format=FORMAT, cursor=self._cursor(), cancelTasks=cancelled)

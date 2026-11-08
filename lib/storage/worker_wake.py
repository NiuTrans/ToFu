"""One authority watch per execution service, with cancellation routed by task.

This is a transport hint port. It neither claims nor settles work. Existing
worker heartbeat deadlines bound long waits; reconnect invalidates observations.
"""
import threading

from lib.storage.commit_wake import StorageCommitWakeSource
from lib.storage.errors import StorageError
from lib.storage.generated_worker_control import FORMAT


class WorkerControlSignals:
    def __init__(self):
        self._lock = threading.Lock()
        self._active = {}

    def subscribe(self, task_id, wake):
        with self._lock:
            self._active.setdefault(task_id, set()).add(wake)

        def close():
            with self._lock:
                events = self._active[task_id]
                events.remove(wake)
                if not events:
                    del self._active[task_id]
        return close

    def refresh(self, task_ids):
        with self._lock:
            groups = self._active.values() if task_ids is None else (
                self._active.get(task_id, ()) for task_id in task_ids)
            for events in groups:
                for event in events:
                    event.set()


class StorageWorkerWakeSource(StorageCommitWakeSource):
    def __init__(self, client_factory, policy):
        super().__init__(client_factory, watch_method='worker_watch',
            interval_ms=policy['heartbeatIntervalMilliseconds'],
            backoff_ms=policy['failureBackoffMilliseconds'],
            thread_name='durable-worker-wake')
        self.signals = WorkerControlSignals()

    def _validate(self, response):
        if (not isinstance(response, dict) or set(response) != {'format', 'cursor', 'cancelTasks'}
                or response['format'] != FORMAT or not isinstance(response['cursor'], dict)
                or set(response['cursor']) != {'epoch', 'queue', 'cancel'}
                or not isinstance(response['cursor']['epoch'], str)
                or any(type(response['cursor'][key]) is not int or not 0 <= response['cursor'][key] < 1 << 64
                       for key in ('queue', 'cancel'))
                or (response['cancelTasks'] is not None and (
                    not isinstance(response['cancelTasks'], list)
                    or not all(isinstance(value, str) and value for value in response['cancelTasks'])))):
            raise StorageError('database_protocol_error', 'Invalid worker watch response')

    def _observe(self, cursor, response, hint):
        current = response['cursor']
        if cursor is None or current['epoch'] != cursor['epoch'] or current['queue'] != cursor['queue']:
            hint('authority-commit')
        if cursor is None or current['epoch'] != cursor['epoch']:
            self.signals.refresh(None)
        elif response['cancelTasks'] is None or response['cancelTasks']:
            self.signals.refresh(response['cancelTasks'])

    def _interrupted(self, hint):
        self.signals.refresh(None)

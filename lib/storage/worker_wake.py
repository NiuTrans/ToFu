"""One authority watch per execution service, with cancellation routed by task.

This is a transport hint port. It neither claims nor settles work. Existing
worker heartbeat deadlines bound long waits; reconnect invalidates observations.
"""
import threading
import time

from lib.log import get_logger
from lib.storage.errors import StorageError
from lib.storage.generated_worker_control import FORMAT

logger = get_logger(__name__)


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


class StorageWorkerWakeSource:
    def __init__(self, client_factory, policy):
        self._factory, self._policy = client_factory, policy
        self.signals = WorkerControlSignals()
        self._stop = threading.Event()
        self._thread = None

    def start(self, hint):
        # Enroll before the first queue read. A commit in the startup gap is
        # returned by the next watch instead of relying on a local producer.
        cursor = self._watch(None)['cursor']
        thread = threading.Thread(target=self._run, args=(cursor, hint),
                                  name='durable-worker-wake', daemon=True)
        self._thread = thread
        thread.start()
        return self.close

    def _watch(self, cursor):
        response = self._factory().worker_watch(cursor=cursor,
            deadline_unix_ms=int(time.time() * 1000) + self._policy['heartbeatIntervalMilliseconds'])
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
        return response

    def _run(self, cursor, hint):
        while not self._stop.is_set():
            try:
                response = self._watch(cursor)
                current = response['cursor']
                if cursor is None or current['epoch'] != cursor['epoch'] or current['queue'] != cursor['queue']:
                    hint('authority-commit')
                if cursor is None or current['epoch'] != cursor['epoch']:
                    self.signals.refresh(None)
                elif response['cancelTasks'] is None or response['cancelTasks']:
                    self.signals.refresh(response['cancelTasks'])
                cursor = current
            except Exception as error:
                logger.warning('[DurableWorker] authority watch interrupted: %s', type(error).__name__)
                cursor = None
                self.signals.refresh(None)
                # The established failure policy owns retries. No idle DB poll.
                self._stop.wait(self._policy['failureBackoffMilliseconds'] / 1000)

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

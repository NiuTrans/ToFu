"""Lifecycle of one cancellable authority watch; domains interpret hints.

The owning application supplies existing deadline and failure policies. No
semantic polling, notification history or per-subscription thread is created.
"""
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


class StorageCommitWakeSource:
    def __init__(self, client_factory, *, watch_method, interval_ms, backoff_ms, thread_name):
        from tofu_db._request_cancellation import RequestCancellation
        self._factory, self._watch_method = client_factory, watch_method
        self._interval_ms, self._backoff_ms = interval_ms, backoff_ms
        self._thread_name = thread_name
        self._stop = threading.Event()
        self._thread = None
        self._lifecycle_lock = threading.Lock()
        self._cancellation = RequestCancellation()

    def start(self, hint):
        # Enroll before the caller's first durable read. Gaps invalidate on
        # the next watch; a notification need not have a live waiter to count.
        try:
            cursor = self._watch(None)['cursor']
        except Exception:
            if not self._stop.is_set():
                raise
            return self.close
        with self._lifecycle_lock:
            if self._stop.is_set():
                return self.close
            thread = threading.Thread(target=self._run, args=(cursor, hint),
                                      name=self._thread_name, daemon=True)
            thread.start()
            self._thread = thread
        return self.close

    def _watch(self, cursor):
        response = getattr(self._factory(), self._watch_method)(cursor=cursor,
            deadline_unix_ms=time.time_ns() // 1_000_000 + self._interval_ms,
            cancellation=self._cancellation)
        self._validate(response)
        return response

    def _run(self, cursor, hint):
        while not self._stop.is_set():
            try:
                response = self._watch(cursor)
                self._observe(cursor, response, hint)
                cursor = response['cursor']
            except Exception as error:
                if self._stop.is_set():
                    return
                logger.warning('[%s] authority watch interrupted: %s',
                               self._thread_name, type(error).__name__)
                cursor = None
                self._interrupted(hint)
                self._stop.wait(self._backoff_ms / 1000)

    def _validate(self, response):
        raise NotImplementedError

    def _observe(self, cursor, response, hint):
        raise NotImplementedError

    def _interrupted(self, hint):
        raise NotImplementedError

    def interrupt(self):
        self._stop.set()
        self._cancellation.cancel()

    def close(self):
        self.interrupt()
        with self._lifecycle_lock:
            thread = self._thread
        if thread is not None:
            thread.join()

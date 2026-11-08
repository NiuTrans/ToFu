"""Ephemeral commit generations and waiter-local hints, without database IO.

Worker and conversation controls share enrollment, gap detection and teardown.
Wire formats and interpretation remain with their domain control owners.
"""
import threading
import time
import uuid

from lib.storage.errors import StorageError


class CommitWatch:
    def __init__(self, generations, *, format, hint_key, hint_generation, encode_hints=sorted):
        self._condition = threading.Condition()
        self._epoch = str(uuid.uuid4())
        self._generations = dict.fromkeys(generations, 0)
        self._format, self._hint_key = format, hint_key
        self._hint_generation, self._encode_hints = hint_generation, encode_hints
        self._waiters = {}
        self._closed = False

    def _cursor(self):
        return dict(epoch=self._epoch, **self._generations)

    def publish_changes(self, changed, hints):
        if not changed:
            return
        with self._condition:
            if self._closed:
                return
            if max(self._generations.values()) == (1 << 64) - 1:
                self._epoch = str(uuid.uuid4())
                self._generations = dict.fromkeys(self._generations, 0)
            for key in changed:
                self._generations[key] += 1
            for observed in self._waiters.values():
                observed.update(hints)
            self._condition.notify_all()

    def close(self):
        with self._condition:
            self._closed = True
            self._condition.notify_all()

    def watch(self, payload, deadline_at):
        if not isinstance(payload, dict) or set(payload) != {'cursor'}:
            raise StorageError('database_protocol_error', 'Invalid commit watch request')
        cursor = payload['cursor']
        if cursor is not None and (
            not isinstance(cursor, dict) or set(cursor) != {'epoch', *self._generations}
            or not isinstance(cursor['epoch'], str)
            or any(type(cursor[k]) is not int or not 0 <= cursor[k] < 1 << 64
                   for k in self._generations)
        ):
            raise StorageError('database_protocol_error', 'Invalid commit watch cursor')
        with self._condition:
            current = self._cursor()
            observed = None
            if cursor == current and not self._closed:
                waiter, hints = object(), set()
                self._waiters[waiter] = hints
                try:
                    self._condition.wait_for(
                        lambda: self._closed or cursor != self._cursor(),
                        max(0, deadline_at - time.monotonic()))
                    if cursor['epoch'] == self._epoch:
                        observed = self._encode_hints(hints)
                finally:
                    del self._waiters[waiter]
            elif (cursor is not None and cursor['epoch'] == current['epoch']
                  and cursor[self._hint_generation] == current[self._hint_generation]):
                observed = []
            if self._closed:
                raise StorageError('database_unavailable', 'Commit control closed')
            return {'format': self._format, 'cursor': self._cursor(), self._hint_key: observed}

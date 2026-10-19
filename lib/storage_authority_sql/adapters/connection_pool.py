"""Connection lease ownership shared by selected storage adapters.

Acquire creates a connection immediately when idle reuse is empty. All borrowed
and connecting leases remain owned through release. Close fences new leases,
joins borrowers/preparers, then closes idle handles. No database paths, SQL,
backend choice or semantic transactions belong here; owners supply a factory.
"""
from __future__ import annotations

from dataclasses import dataclass
import queue
import threading
import time

from lib.storage.errors import StorageError

from lib.log import get_logger

logger = get_logger(__name__)


@dataclass(slots=True)
class ConnectionSlot:
    connection: object
    created_at: float
    last_used_at: float
    leased: bool = False
    owner: object | None = None
    releasing: bool = False


class DemandConnectionPool:
    def __init__(self, connect, *, idle_reuse_hint, max_lifetime_s=float('inf'),
                 idle_lifetime_s=float('inf'), classify_error=None, driver_errors=(), reset=None):
        self.connect = connect
        self.idle_reuse_hint = max(1, int(idle_reuse_hint))
        self.max_lifetime_s = max_lifetime_s
        self.idle_lifetime_s = idle_lifetime_s
        self.classify_error = classify_error
        self.driver_errors = driver_errors
        self.reset = reset
        self.idle = queue.Queue()
        self.condition = threading.Condition()
        self.active = self.peak = self.rotations = self.connections_created = 0
        self.closed = False
        self._close_lock = threading.RLock()

    def _raise_error(self, error, deadline_at):
        if isinstance(error, self.driver_errors) and self.classify_error is not None:
            raise self.classify_error(error, deadline_at) from error
        raise error

    def acquire(self, deadline_at):
        with self.condition:
            if self.closed:
                raise StorageError('database_unavailable', 'Storage connection is not available')
            if time.monotonic() >= deadline_at:
                raise StorageError('database_timeout', 'Storage connection deadline expired', True, 25)
            try:
                slot = self.idle.get_nowait()
            except queue.Empty:
                slot = None
            if slot is not None and (slot.owner not in (None, self) or slot.leased or slot.releasing):
                raise RuntimeError('Storage idle connection belongs to another lease owner')
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            now = time.monotonic()
            if slot is None:
                slot = ConnectionSlot(self.connect(deadline_at), now, now)
                with self.condition:
                    self.connections_created += 1
            elif (now - slot.created_at >= self.max_lifetime_s
                    or now - slot.last_used_at >= self.idle_lifetime_s):
                slot.connection.close()
                slot = ConnectionSlot(self.connect(deadline_at), now, now)
                with self.condition:
                    self.connections_created += 1
                    self.rotations += 1
            if time.monotonic() >= deadline_at:
                raise StorageError('database_timeout', 'Storage connection deadline expired', True, 25)
            slot.owner = self
            slot.leased = True
            return slot
        except BaseException as error:
            try:
                if slot is not None:
                    slot.connection.close()
            finally:
                with self.condition:
                    self.active -= 1
                    self.condition.notify_all()
            self._raise_error(error, deadline_at)

    def release(self, slot, *, retire=False):
        with self.condition:
            if slot.owner is not self or not slot.leased or slot.releasing:
                raise RuntimeError('Storage connection lease is not owned or was already released')
            slot.releasing = True
        cleanup_error = None
        try:
            if not retire:
                if self.reset is not None:
                    self.reset(slot.connection)
                slot.last_used_at = time.monotonic()
        except BaseException as error:
            logger.debug('release failure handled: %s', type(error).__name__)
            retire = True
            cleanup_error = error
        with self.condition:
            if retire:
                self.rotations += 1
            if not slot.leased:
                raise RuntimeError('Storage connection lease was already released')
            if not retire and not self.closed and self.idle.qsize() < self.idle_reuse_hint:
                slot.leased = False
                slot.releasing = False
                self.idle.put(slot)
                self.active -= 1
                self.condition.notify_all()
                return
        try:
            slot.connection.close()
        finally:
            with self.condition:
                slot.leased = False
                slot.releasing = False
                self.active -= 1
                self.condition.notify_all()
        if cleanup_error is not None:
            self._raise_error(cleanup_error, float('inf'))

    def begin_close(self):
        with self.condition:
            self.closed = True
            self.condition.notify_all()

    def close(self):
        with self._close_lock:
            self.begin_close()
            with self.condition:
                while self.active:
                    self.condition.wait()
            while True:
                try:
                    slot = self.idle.get_nowait()
                except queue.Empty:
                    break
                slot.connection.close()

    def snapshot(self):
        with self.condition:
            return {'idle': self.idle.qsize(), 'active': self.active, 'peak': self.peak,
                    'idle_reuse_hint': self.idle_reuse_hint, 'capacity': 0,
                    'rotations': self.rotations, 'connections_created': self.connections_created}

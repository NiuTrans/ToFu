"""Required terminal handoff, independent of task persistence and observers.

An application binds this port only after committing its task ownership.
Delivery is serialized, acknowledges only success, and retries the same task
identity after a lost acknowledgement. The receiver must be idempotent.
No repository, worker or process-global registry lives in this module.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import threading
import hashlib
import json
from copy import deepcopy


TERMINAL_STATUSES = frozenset({'done', 'error', 'aborted', 'interrupted'})


@dataclass
class TerminalDelivery:
    task_id: str
    owner_user_id: int
    receiver: Callable[[dict], object]
    persistence_preparer: Callable[[dict, str, int], dict] | None = None
    _prepared: dict | None = field(default=None, init=False)
    _preparation_identity: tuple | None = field(default=None, init=False)
    _preparing: bool = field(default=False, init=False)
    _acknowledged: bool = field(default=False, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _delivering: bool = field(default=False, init=False)

    def prepare_persistence(self, task: dict, content: str, content_epoch: int) -> dict | None:
        """Freeze an opaque bounded application payload before the owned result CAS."""
        if task.get('id') != self.task_id or task.get('_userId') != self.owner_user_id:
            raise ValueError('terminal delivery identity changed')
        if task.get('status') not in TERMINAL_STATUSES or self.persistence_preparer is None:
            return None
        identity = (task['status'], content_epoch, hashlib.sha256(content.encode('utf-8')).hexdigest())
        with self._lock:
            if self._prepared is not None:
                if identity != self._preparation_identity:
                    raise ValueError('terminal persistence input changed')
                return deepcopy(self._prepared)
            if self._preparing:
                raise RuntimeError('recursive terminal persistence preparation')
            self._preparing = True
            try:
                value = self.persistence_preparer(task, content, content_epoch)
                if not isinstance(value, dict):
                    raise ValueError('terminal persistence payload must be an object')
                encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
                if len(encoded.encode('utf-8')) > 1024 * 1024:
                    raise ValueError('terminal persistence payload exceeds bound')
                self._prepared = json.loads(encoded)
                self._preparation_identity = identity
                return deepcopy(self._prepared)
            finally:
                self._preparing = False

    def deliver(self, task: dict) -> None:
        if task.get('id') != self.task_id or task.get('_userId') != self.owner_user_id:
            raise ValueError('terminal delivery identity changed')
        if task.get('status') not in TERMINAL_STATUSES:
            return
        with self._lock:
            if self._acknowledged:
                return
            if self._delivering:
                raise RuntimeError('recursive terminal delivery')
            self._delivering = True
            try:
                if self.receiver(task) is False:
                    raise RuntimeError('terminal receiver did not acknowledge')
                self._acknowledged = True
            finally:
                self._delivering = False


def bind_terminal_delivery(task: dict, receiver: Callable[[dict], object], *,
                           persistence_preparer=None) -> None:
    """Bind server-owned authority once; config cannot install a receiver."""
    if not task.get('id') or type(task.get('_userId')) is not int or task['_userId'] < 1:
        raise ValueError('terminal delivery requires owned task identity')
    if not callable(receiver) or '_terminalDelivery' in task:
        raise ValueError('terminal delivery requires one callable receiver')
    if persistence_preparer is not None and not callable(persistence_preparer):
        raise ValueError('terminal persistence preparer must be callable')
    task['_terminalDelivery'] = TerminalDelivery(task['id'], task['_userId'], receiver, persistence_preparer)


def deliver_terminal(task: dict) -> None:
    delivery = task.get('_terminalDelivery')
    if delivery is not None:
        if not isinstance(delivery, TerminalDelivery):
            raise ValueError('invalid terminal delivery port')
        delivery.deliver(task)


def prepare_terminal_persistence(task: dict, *, content: str, content_epoch: int) -> dict | None:
    delivery = task.get('_terminalDelivery')
    if delivery is None:
        return None
    if not isinstance(delivery, TerminalDelivery):
        raise ValueError('invalid terminal delivery port')
    return delivery.prepare_persistence(task, content, content_epoch)

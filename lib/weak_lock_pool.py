"""Weakly retain per-key thread locks while callers are actively using them.

Entry point: :class:`WeakLockPool`. Dependencies: Python threading and weakref.
Use this for process-local serialization keyed by a high-cardinality identity;
fixed, permanently-known lock sets should remain explicit module constants.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
import threading
from typing import Generic, TypeVar
import weakref


Key = TypeVar("Key", bound=Hashable)
Lock = TypeVar("Lock")


class WeakLockPool(Generic[Key, Lock]):
    """Return one lock per live key without retaining idle keys forever.

    The pool mutation guard makes lookup-and-create atomic. A caller's local
    lock reference keeps the weak value alive from lookup through acquisition;
    once all holders and waiters leave, normal garbage collection removes the
    entry. Lock factories must therefore return weak-referenceable objects.
    """

    def __init__(self, factory: Callable[[], Lock]) -> None:
        self._factory = factory
        self._locks: weakref.WeakValueDictionary[Key, Lock] = (
            weakref.WeakValueDictionary()
        )
        self._guard = threading.Lock()

    def lock_for(self, key: Key) -> Lock:
        """Return the current lock for ``key``, creating it atomically."""
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._factory()
                self._locks[key] = lock
            return lock

    def __len__(self) -> int:
        with self._guard:
            return len(self._locks)

    def __contains__(self, key: object) -> bool:
        with self._guard:
            return key in self._locks


__all__ = ["WeakLockPool"]

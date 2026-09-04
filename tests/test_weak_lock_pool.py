"""Resource and concurrency contracts for weak per-key lock ownership."""

from __future__ import annotations

import gc
import threading
import weakref

import pytest

from lib.weak_lock_pool import WeakLockPool


pytestmark = pytest.mark.unit


def test_pool_shares_live_lock_and_reclaims_idle_key() -> None:
    pool = WeakLockPool(threading.Lock)
    first = pool.lock_for("project-a")
    second = pool.lock_for("project-a")
    reference = weakref.ref(first)

    assert first is second
    assert "project-a" in pool
    assert len(pool) == 1

    del first, second
    gc.collect()

    assert reference() is None
    assert "project-a" not in pool
    assert len(pool) == 0


def test_concurrent_callers_cannot_receive_parallel_locks_for_one_key() -> None:
    pool = WeakLockPool(threading.Lock)
    caller_count = 8
    looked_up = threading.Barrier(caller_count + 1)
    release = threading.Barrier(caller_count + 1)
    identities: list[int] = []

    def lookup() -> None:
        lock = pool.lock_for("shared")
        identities.append(id(lock))
        looked_up.wait(timeout=5)
        release.wait(timeout=5)

    threads = [threading.Thread(target=lookup) for _ in range(caller_count)]
    for thread in threads:
        thread.start()
    looked_up.wait(timeout=5)
    assert len(set(identities)) == 1
    release.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

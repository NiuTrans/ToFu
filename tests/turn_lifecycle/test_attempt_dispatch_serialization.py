"""Attempt dispatch serialization is exact per attempt identity.

A claimed conversation holds its attempt's dispatch lock for its whole
execution, so an unrelated attempt must never wait on it: a fixed pool of
hashed stripes once parked new turns behind unrelated hours-long tasks.
One attempt stays mutually exclusive, and only in-flight keys are retained.
"""
import threading
import uuid

import pytest

from lib import turn_lifecycle

pytestmark = pytest.mark.unit

# Far more identities than any fixed pool of locks could keep apart.
DISTINCT_ATTEMPTS = 512
WAIT_SECONDS = 20


def test_unrelated_attempts_never_wait_for_each_other():
    entered = threading.Barrier(DISTINCT_ATTEMPTS + 1)
    release = threading.Event()
    broken = []

    def dispatch(attempt_id):
        with turn_lifecycle.attempt_dispatch_lock(attempt_id):
            try:
                entered.wait(WAIT_SECONDS)
            except threading.BrokenBarrierError:
                broken.append(attempt_id)
            release.wait(WAIT_SECONDS)

    threads = [threading.Thread(target=dispatch, args=(f'attempt-{index}',), daemon=True)
               for index in range(DISTINCT_ATTEMPTS)]
    for thread in threads:
        thread.start()
    try:
        # Breaks unless every attempt holds its own lock at the same time.
        entered.wait(WAIT_SECONDS)
    finally:
        release.set()
        for thread in threads:
            thread.join(WAIT_SECONDS)
    assert not broken
    assert not any(thread.is_alive() for thread in threads)


def test_one_attempt_is_dispatched_by_one_holder_at_a_time():
    held, release, contender_entered = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with turn_lifecycle.attempt_dispatch_lock('attempt-shared'):
            held.set()
            release.wait(WAIT_SECONDS)

    def contender():
        held.wait(WAIT_SECONDS)
        with turn_lifecycle.attempt_dispatch_lock('attempt-shared'):
            contender_entered.set()

    threads = [threading.Thread(target=holder, daemon=True),
               threading.Thread(target=contender, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        assert held.wait(WAIT_SECONDS)
        assert not contender_entered.wait(0.2)
    finally:
        release.set()
    assert contender_entered.wait(WAIT_SECONDS)
    for thread in threads:
        thread.join(WAIT_SECONDS)


def test_released_attempts_leave_no_retained_lock():
    prefix = f'attempt-{uuid.uuid4().hex}-'
    for index in range(1000):
        with turn_lifecycle.attempt_dispatch_lock(prefix + str(index)):
            pass
    with pytest.raises(RuntimeError, match='released'):
        with turn_lifecycle.attempt_dispatch_lock(prefix + 'failed'):
            raise RuntimeError('released on failure')
    # Bounded retention is the invariant the former fixed pool existed for.
    assert not [key for key in turn_lifecycle._ATTEMPT_DISPATCH_LOCKS if key.startswith(prefix)]


def test_attempt_identity_is_required():
    with pytest.raises(ValueError, match='attempt_id is required'):
        with turn_lifecycle.attempt_dispatch_lock(''):
            pass

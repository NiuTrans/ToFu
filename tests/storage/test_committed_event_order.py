"""Deferred commit notices publish after their critical section, in commit order."""
from __future__ import annotations

import threading

import pytest

from lib.storage.commit_events import (
    CommittedEventOrder,
    defer_committed_events,
    publish_committed_events,
    subscribe_committed_events,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def published():
    batches: list[list[int]] = []
    unsubscribe = subscribe_committed_events(
        lambda batch: batches.append([event['n'] for event in batch]))
    try:
        yield batches
    finally:
        unsubscribe()


def test_deferred_notices_publish_after_the_scope_in_commit_order(published):
    order = CommittedEventOrder()
    with defer_committed_events(order):
        publish_committed_events([{'n': 1}])
        publish_committed_events([{'n': 2}, {'n': 3}])
        assert published == []
    assert published == [[1], [2, 3]]


def test_undeferred_notices_still_publish_before_the_command_returns(published):
    publish_committed_events([{'n': 1}])
    assert published == [[1]]


def test_nested_scopes_publish_once_after_the_outermost_scope(published):
    outer, inner = CommittedEventOrder(), CommittedEventOrder()
    with defer_committed_events(outer):
        publish_committed_events([{'n': 1}])
        with defer_committed_events(outer):
            publish_committed_events([{'n': 2}])
        with defer_committed_events(inner):
            publish_committed_events([{'n': 3}])
        assert published == []
    assert published == [[1], [2], [3]]


def test_a_later_committer_publishes_a_pending_earlier_commit_first(published):
    # Thread A has committed but not yet published when thread B commits and
    # publishes. A naive per-thread deferral would publish B's notice first.
    order = CommittedEventOrder()
    a_committed, b_finished = threading.Event(), threading.Event()

    def commit_b():
        assert a_committed.wait(5)
        with defer_committed_events(order):
            publish_committed_events([{'n': 2}])
        b_finished.set()

    thread_b = threading.Thread(target=commit_b)
    thread_b.start()
    with defer_committed_events(order):
        publish_committed_events([{'n': 1}])
        a_committed.set()
        assert b_finished.wait(5)
    thread_b.join(5)
    assert published == [[1], [2]]


def test_concurrent_critical_sections_publish_in_commit_order(published):
    order = CommittedEventOrder()
    critical_section = threading.RLock()
    sequence = iter(range(1, 10_001))
    start = threading.Barrier(8)

    def commit_many():
        start.wait()
        for _ in range(250):
            with defer_committed_events(order):
                with critical_section:
                    publish_committed_events([{'n': next(sequence)}])

    threads = [threading.Thread(target=commit_many) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    flattened = [value for batch in published for value in batch]
    assert flattened == list(range(1, 2001))


def test_a_failed_subscriber_does_not_strand_later_notices(published):
    def failing(batch):
        raise RuntimeError('subscriber failure')

    unsubscribe = subscribe_committed_events(failing)
    order = CommittedEventOrder()
    try:
        with defer_committed_events(order):
            publish_committed_events([{'n': 1}])
            publish_committed_events([{'n': 2}])
    finally:
        unsubscribe()
    assert published == [[1], [2]]


def test_a_subscriber_commit_during_publication_keeps_order(published):
    order = CommittedEventOrder()
    nested_done = []

    def committing_subscriber(batch):
        if batch[0]['n'] == 1 and not nested_done:
            nested_done.append(True)
            with defer_committed_events(order):
                publish_committed_events([{'n': 3}])

    unsubscribe = subscribe_committed_events(committing_subscriber)
    try:
        with defer_committed_events(order):
            publish_committed_events([{'n': 1}])
            publish_committed_events([{'n': 2}])
    finally:
        unsubscribe()
    assert published == [[1], [2], [3]]


def test_scope_exit_after_failure_still_publishes_committed_notices(published):
    order = CommittedEventOrder()
    with pytest.raises(RuntimeError, match='after commit'):
        with defer_committed_events(order):
            publish_committed_events([{'n': 1}])
            raise RuntimeError('after commit')
    assert published == [[1]]

"""Finite, owner-fair resource contracts for knowledge vision enrichment."""

from __future__ import annotations

from collections import defaultdict
import threading
import time

import pytest


pytestmark = pytest.mark.unit


def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('condition did not become true before timeout')


def test_owner_turns_are_round_robin_and_threads_retire():
    from lib.knowledge.enrichment_lane import OwnerFairEnrichmentLane

    first_started = threading.Event()
    release_first = threading.Event()
    calls: defaultdict[int, int] = defaultdict(int)
    order: list[int] = []

    def process(owner_id: int, _stop_event: threading.Event) -> bool:
        calls[owner_id] += 1
        order.append(owner_id)
        if len(order) == 1:
            first_started.set()
            assert release_first.wait(1.0)
        return calls[owner_id] < 2

    lane = OwnerFairEnrichmentLane(
        max_workers=1,
        owner_capacity=3,
        idle_seconds=0.02,
        processor=process,
    )
    try:
        assert lane.schedule(1)
        assert first_started.wait(1.0)
        assert lane.schedule(2)
        assert lane.schedule(3)
        release_first.set()
        _wait_until(lambda: lane.snapshot()['retainedOwners'] == 0)
        assert order[:3] == [1, 2, 3]
        assert calls == {1: 2, 2: 2, 3: 2}
        _wait_until(lambda: lane.snapshot()['residentThreads'] == 0)
        assert lane.snapshot()['retiredThreads'] == 1
    finally:
        release_first.set()
        lane.stop(timeout=1.0)


def test_workers_and_retained_owners_have_independent_hard_bounds():
    from lib.knowledge.enrichment_lane import (
        KnowledgeEnrichmentCapacityExceeded,
        OwnerFairEnrichmentLane,
    )

    release = threading.Event()
    two_active = threading.Event()
    lock = threading.Lock()
    active = 0
    peak_active = 0

    def process(_owner_id: int, _stop_event: threading.Event) -> bool:
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
            if active == 2:
                two_active.set()
        assert release.wait(1.0)
        with lock:
            active -= 1
        return False

    lane = OwnerFairEnrichmentLane(
        max_workers=2,
        owner_capacity=3,
        idle_seconds=0.01,
        processor=process,
    )
    try:
        assert lane.schedule(1)
        assert lane.schedule(2)
        assert two_active.wait(1.0)
        assert lane.schedule(3)
        with pytest.raises(KnowledgeEnrichmentCapacityExceeded):
            lane.schedule(4)
        snapshot = lane.snapshot()
        assert snapshot['residentThreads'] == 2
        assert snapshot['retainedOwners'] == 3
        assert snapshot['rejected'] == 1
        release.set()
        _wait_until(lambda: lane.snapshot()['retainedOwners'] == 0)
        assert peak_active == 2
    finally:
        release.set()
        lane.stop(timeout=1.0)


def test_stopping_a_queued_owner_does_not_run_its_callback():
    from lib.knowledge.enrichment_lane import OwnerFairEnrichmentLane

    first_started = threading.Event()
    release = threading.Event()
    called: list[int] = []

    def process(owner_id: int, _stop_event: threading.Event) -> bool:
        called.append(owner_id)
        first_started.set()
        assert release.wait(1.0)
        return False

    lane = OwnerFairEnrichmentLane(
        max_workers=1,
        owner_capacity=2,
        idle_seconds=0.01,
        processor=process,
    )
    try:
        assert lane.schedule(1)
        assert first_started.wait(1.0)
        assert lane.schedule(2)
        assert lane.stop(owner_user_id=2, timeout=0.1)
        release.set()
        _wait_until(lambda: lane.snapshot()['retainedOwners'] == 0)
        assert called == [1]
        assert lane.snapshot()['cancelled'] == 1
    finally:
        release.set()
        lane.stop(timeout=1.0)


def test_explicit_resource_overrides_remain_hard_capped(monkeypatch):
    from lib.knowledge.resource_policy import (
        knowledge_enrichment_owner_capacity,
        knowledge_enrichment_worker_idle_seconds,
        knowledge_enrichment_workers,
    )

    monkeypatch.setenv('TOFU_KNOWLEDGE_ENRICH_WORKERS', '9999')
    monkeypatch.setenv('TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY', '9999')
    monkeypatch.setenv(
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', '999999')

    assert knowledge_enrichment_workers() == 16
    assert knowledge_enrichment_owner_capacity() == 512
    assert knowledge_enrichment_worker_idle_seconds() == 86_400


def test_zero_idle_override_keeps_workers_for_latency_sensitive_hosts(
        monkeypatch):
    from lib.knowledge.resource_policy import (
        knowledge_enrichment_worker_idle_seconds,
    )

    monkeypatch.setenv('TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', '0')

    assert knowledge_enrichment_worker_idle_seconds() == 0.0

"""Demand-driven owner lifecycle contracts for knowledge vision enrichment."""

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
    ensure = lane._ensure_workers_locked
    lane._ensure_workers_locked = lambda: ensure() if not lane._threads else None
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


def test_independent_owners_start_without_count_quota():
    from lib.knowledge.enrichment_lane import OwnerFairEnrichmentLane

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
            if active == 64:
                two_active.set()
        assert release.wait(5.0)
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
        for owner in range(1, 65):
            assert lane.schedule(owner)
        assert two_active.wait(3.0)
        assert not lane.schedule(1)
        snapshot = lane.snapshot()
        assert snapshot['residentThreads'] == snapshot['active'] == 64
        assert snapshot['retainedOwners'] == 64
        assert snapshot['ownerCapacity'] == snapshot['rejected'] == 0
        release.set()
        _wait_until(lambda: lane.snapshot()['retainedOwners'] == 0)
        assert peak_active == 64
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
        assert release.wait(5.0)
        return False

    lane = OwnerFairEnrichmentLane(
        max_workers=1,
        owner_capacity=2,
        idle_seconds=0.01,
        processor=process,
    )
    ensure = lane._ensure_workers_locked
    lane._ensure_workers_locked = lambda: ensure() if not lane._threads else None
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
        knowledge_enrichment_worker_idle_seconds,
        knowledge_enrichment_workers,
    )

    monkeypatch.setenv('TOFU_KNOWLEDGE_ENRICH_WORKERS', '9999')
    monkeypatch.setenv('TOFU_KNOWLEDGE_ENRICH_OWNER_CAPACITY', '9999')
    monkeypatch.setenv(
        'TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', '999999')

    assert knowledge_enrichment_workers() == 16
    from lib.knowledge import resource_policy
    assert not hasattr(resource_policy, "knowledge_enrichment_owner_capacity")
    assert knowledge_enrichment_worker_idle_seconds() == 86_400


def test_zero_idle_override_keeps_workers_for_latency_sensitive_hosts(
        monkeypatch):
    from lib.knowledge.resource_policy import (
        knowledge_enrichment_worker_idle_seconds,
    )

    monkeypatch.setenv('TOFU_KNOWLEDGE_ENRICH_WORKER_IDLE_SECONDS', '0')

    assert knowledge_enrichment_worker_idle_seconds() == 0.0


def test_resume_pages_all_opted_in_owners_and_detects_stalled_cursor(monkeypatch):
    from lib.identity import PrincipalContext
    from lib.knowledge import enrichment, repository
    principal = PrincipalContext.system(subject_id='test', scopes={'knowledge:maintain'})
    admitted = []
    requests = []
    def inventory(*, principal, limit, after_user_id):
        requests.append((limit, after_user_id))
        return list(range(after_user_id + 1, min(1200, after_user_id + limit) + 1))
    monkeypatch.setattr(repository, 'visual_enrichment_owner_ids', inventory)
    monkeypatch.setattr(enrichment, 'start_visual_enrichment',
                        lambda *, user_id: admitted.append(user_id) or True)
    assert enrichment.resume_visual_enrichment(principal=principal) == 1200
    assert admitted == list(range(1, 1201))
    assert requests == [(512, 0), (512, 512), (512, 1024), (512, 1200)]
    monkeypatch.setattr(repository, 'visual_enrichment_owner_ids', lambda **kwargs: [1])
    with pytest.raises(RuntimeError, match='did not advance'):
        enrichment.resume_visual_enrichment(principal=principal)


def test_new_owner_start_failure_preserves_existing_owner(monkeypatch):
    from lib.knowledge.enrichment_lane import OwnerFairEnrichmentLane
    entered = threading.Event()
    release = threading.Event()
    def process(owner, stop):
        entered.set()
        assert release.wait(3)
        return False
    lane = OwnerFairEnrichmentLane(max_workers=1, idle_seconds=.01, processor=process)
    try:
        assert lane.schedule(1) and entered.wait(1)
        def fail(thread):
            raise OSError('actual thread start failure')
        with monkeypatch.context() as fault:
            fault.setattr(threading.Thread, 'start', fail)
            with pytest.raises(OSError, match='actual thread start failure'):
                lane.schedule(2)
        snapshot = lane.snapshot()
        assert snapshot['retainedOwners'] == snapshot['active'] == 1
        assert snapshot['queued'] == 0
        assert snapshot['accepted'] == snapshot['rejected'] == 1
    finally:
        release.set()
        assert lane.stop(timeout=2)


def test_same_owner_stop_revive_never_duplicates_active_processor():
    from lib.knowledge.enrichment_lane import OwnerFairEnrichmentLane
    entered = threading.Event()
    stopped = threading.Event()
    release = threading.Event()
    peer = threading.Event()
    calls = defaultdict(int)
    def process(owner, stop):
        calls[owner] += 1
        if owner == 1 and calls[owner] == 1:
            entered.set()
            assert stop.wait(3)
            stopped.set()
            assert release.wait(3)
        if owner == 2:
            peer.set()
        return False
    lane = OwnerFairEnrichmentLane(max_workers=1, idle_seconds=.01, processor=process)
    try:
        assert lane.schedule(1) and entered.wait(1)
        assert not lane.stop(owner_user_id=1, timeout=.01)
        assert stopped.wait(1)
        assert lane.schedule(1)
        assert lane.schedule(2) and peer.wait(1)
        assert calls[1] == 1
        release.set()
        _wait_until(lambda: lane.snapshot()['retainedOwners'] == 0)
        assert calls == {1: 2, 2: 1}
    finally:
        release.set()
        lane.stop(timeout=2)

"""Exact causal timing, merged FIFO coverage and observer failure isolation."""
import json
import threading

import pytest

from lib.storage.errors import StorageError
from lib.tasks_pkg.event_delivery import DeliveryLane, EventDeliveryCoordinator, ProgressFrame
from lib.tasks_pkg.event_delivery_profile import DeliveryProfiler

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.now = 100

    def __call__(self):
        return self.now


def test_per_frame_partition_and_attempt_failures_are_distinct_populations():
    clock = Clock()
    profile = DeliveryProfiler(1, clock_ns=clock)
    sample = profile.accept(1)
    clock.now = 130
    profile.accept_frame(sample, 2)
    clock.now = 200
    profile.dispatch(sample)
    clock.now = 210
    start = profile.attempt_start(sample)
    clock.now = 240
    profile.attempt_end(sample, start, True)
    clock.now = 290
    start = profile.attempt_start(sample)
    clock.now = 350
    profile.attempt_end(sample, start, False)
    clock.now = 360
    profile.acknowledge(sample)
    profile.publication(sample, 1, 370, 375, 400, False)
    profile.publication(sample, 2, 410, 420, 450, True)
    profile.finish(sample, 'committed')
    result = profile.snapshot()
    stages = result['stages']
    assert stages['fifo_wait']['totalNanos'] == 170, 'each merged frame keeps its own arrival'
    assert stages['storage_attempt']['count'] == 2
    assert stages['storage_attempt']['totalNanos'] == 90, 'includes failed calls'
    assert stages['attempt_gap']['totalNanos'] == 50
    assert stages['persistence_wall']['totalNanos'] == 160
    event_partition = (stages['fifo_wait']['totalNanos'] + 2 * stages['persistence_wall']['totalNanos']
        + sum(stages[name]['totalNanos'] for name in ('ack_to_visibility', 'visibility_update', 'publish_callback')))
    assert event_partition == stages['accept_to_publish']['totalNanos'] == 620
    assert result['failedAttempts'] == result['slowSamples'][0]['failedAttempts'] == 1
    assert result['slowSamples'][0]['publishErrors'] == 1
    assert result['activeSamples'] == result['activeFrameAnchors'] == result['invalidClocks'] == 0


def test_sampling_bounded_summaries_and_live_stalls_keep_coverage_visible():
    clock = Clock()
    profile = DeliveryProfiler(2, clock_ns=clock)
    for sequence in range(1000):
        sample = profile.accept(sequence)
        if sample is not None:
            profile.finish(sample, 'revoked')
    pending = profile.accept(1001)
    clock.now = 2000
    result = profile.snapshot()
    assert result['seenBatches'] == 1001 and result['selectedBatches'] == 501
    assert result['completedSamples'] == 500 and result['activeSamples'] == 1
    assert len(result['slowSamples']) == 32
    assert result['oldestActive'] == [dict(sample=pending.number, events=1, attempts=0,
        ageNanos=1900, persistenceStarted=False)]
    assert result['metadataBytes'] < 100_000
    assert all('frames' not in row for row in result['slowSamples']), 'completed summaries do not retain per-frame clocks'


def test_invalid_clock_is_reported_instead_of_a_fast_success():
    clock = Clock()
    profile = DeliveryProfiler(1, clock_ns=clock)
    sample = profile.accept(1)
    clock.now = 50
    profile.dispatch(sample)
    result = profile.snapshot()
    assert result['invalidClocks'] == 1
    assert result['stages']['fifo_wait']['count'] == 0


def test_merged_frames_have_real_fifo_anchors_and_push_error_never_repeats_commit():
    owner = EventDeliveryCoordinator(workers=1, profile_every=1, idle_timeout=.01)
    lane = DeliveryLane({'id': 'private-owner-task'})
    entered, release = threading.Event(), threading.Event()
    persisted, pushes = [], []

    def blocked():
        entered.set()
        assert release.wait(3)
        persisted.append([1])

    def publish(sequence):
        pushes.append(sequence)
        if sequence == 2:
            raise ValueError('private-payload-error')

    try:
        owner.submit(lane, 1, 64, blocked, lambda: publish(1))
        assert entered.wait(2)
        identity = object()
        for sequence in (2, 3):
            frame = ProgressFrame(sequence, {}, lambda seq=sequence: publish(seq))
            owner.submit(lane, sequence, 64, lambda: None, frame.publish,
                projection_identity=identity, progress_frame=frame,
                persist_progress=lambda frames: persisted.append([f.sequence for f in frames]),
                progress_wire_bytes=64, progress_payload_bytes=64)
        assert not pushes
        pending = owner.snapshot()['performance_profile']
        assert pending['activeFrameAnchors'] == 3
        release.set()
        owner.flush(lane)
        result = owner.snapshot()['performance_profile']
        assert persisted == [[1], [2, 3]] and pushes == [1, 2, 3]
        assert result['stages']['fifo_wait']['count'] == 3
        assert result['stages']['storage_attempt']['count'] == 2
        assert result['stages']['accept_to_publish']['count'] == 3
        assert result['completedSamples'] == 2 and result['activeFrameAnchors'] == 0
        assert sum(s['publishErrors'] for s in result['slowSamples']) == 1
        assert 'private-' not in json.dumps(result)
    finally:
        release.set()
        assert owner.close(timeout=3)


def test_profiler_failure_cannot_turn_a_commit_into_a_retry(monkeypatch):
    owner = EventDeliveryCoordinator(workers=1, profile_every=1, idle_timeout=.01)
    lane = DeliveryLane({'id': 'observer-failure'})
    commits, pushes = [], []
    monkeypatch.setattr(owner.profiler, 'attempt_end', lambda *args: (_ for _ in ()).throw(RuntimeError('observer')))
    try:
        owner.submit(lane, 1, 64, lambda: commits.append(1), lambda: pushes.append(1))
        owner.flush(lane)
        result = owner.snapshot()['performance_profile']
        assert commits == pushes == [1]
        assert result['observerErrors'] == 1 and result['activeSamples'] == 0
    finally:
        assert owner.close(timeout=3)


def test_failed_head_is_visible_and_repair_preserves_original_acceptance():
    owner = EventDeliveryCoordinator(workers=1, profile_every=1, idle_timeout=.01)
    lane = DeliveryLane({'id': 'repair'})
    available = threading.Event()
    entered = threading.Event()
    pushes = []

    def persist():
        entered.set()
        if not available.is_set():
            raise StorageError('database_integrity', 'injected permanent failure')

    try:
        owner.submit(lane, 1, 64, persist, lambda: pushes.append(1))
        assert entered.wait(2)
        with pytest.raises(Exception, match='delivery failed'):
            owner.flush(lane, recover=False)
        result = owner.snapshot()['performance_profile']
        assert result['activeSamples'] == 1 and result['failedPersistenceRounds'] == 1
        assert result['barrierErrors'] == 1 and not pushes
        available.set()
        owner.flush(lane)
        result = owner.snapshot()['performance_profile']
        assert pushes == [1] and result['activeSamples'] == 0
        assert result['stages']['fifo_wait']['count'] == 1, 'repair does not invent another accepted frame'
        assert result['slowSamples'][0]['attempts'] == 2
        assert result['slowSamples'][0]['failedAttempts'] == 1
    finally:
        available.set()
        assert owner.close(timeout=3)


def test_disabled_profile_adds_no_profile_clock_or_metadata(monkeypatch):
    monkeypatch.setenv('TOFU_EVENT_DELIVERY_PROFILE_EVERY', '0')
    owner = EventDeliveryCoordinator(workers=1, idle_timeout=.01)
    lane = DeliveryLane({'id': 'disabled'})
    monkeypatch.setattr('lib.tasks_pkg.event_delivery_profile.time.monotonic_ns',
        lambda: (_ for _ in ()).throw(AssertionError('profile clock must stay off')))
    try:
        owner.submit(lane, 1, 64, lambda: None, lambda: None)
        owner.flush(lane)
        assert owner.profiler is None
        assert owner.snapshot()['performance_profile'] == {'enabled': False}
    finally:
        assert owner.close(timeout=3)


def test_prometheus_histogram_is_cumulative_and_has_no_user_labels():
    from lib.tasks_pkg.event_delivery_profile_metrics import emit_delivery_profile
    from routes.metrics import _emit_counter, _emit_gauge
    profile = DeliveryProfiler(1)
    for duration in (0, 1, 2, 3, 1 << 63):
        profile._record('fifo_wait', duration)
    out = []
    emit_delivery_profile(out, profile.snapshot(), _emit_counter, _emit_gauge)
    rows = [s for s in out if '_stage_seconds_bucket{stage="fifo_wait"' in s]
    counts = [int(row.split()[-1]) for row in rows]
    assert len(counts) == 64 and counts == sorted(counts)
    assert counts[0] == 2 and counts[-1] == 5
    assert 'le="+Inf"' in rows[-1]
    assert '_stage_seconds_count{stage="fifo_wait"} 5' in '\n'.join(out)


def test_authority_revocation_reclaims_sampled_tail_without_fabricating_acks():
    from lib.tasks_pkg.event_delivery import EventDeliveryError, EventDeliveryRevoked
    owner = EventDeliveryCoordinator(workers=1, profile_every=1, idle_timeout=.01)
    lane = DeliveryLane({'id': 'revoked'})
    entered, release = threading.Event(), threading.Event()
    def persist():
        entered.set()
        assert release.wait(3)
        raise EventDeliveryRevoked('authority revoked')
    try:
        owner.submit(lane, 1, 64, persist, lambda: pytest.fail('unacknowledged push'))
        assert entered.wait(2)
        owner.submit(lane, 2, 64, lambda: pytest.fail('revoked follower entered'), lambda: None)
        release.set()
        owner.flush(lane, recover=False)
        result = owner.snapshot()['performance_profile']
        assert result['activeSamples'] == result['activeFrameAnchors'] == 0
        assert result['acceptedFrameAnchors'] == 2 and result['acknowledgedFrameAnchors'] == 0
        assert result['outcomes']['revoked'] == 2
        assert result['stages']['storage_attempt']['count'] == 1
        assert result['stages']['publish_callback']['count'] == 0
    finally:
        release.set()
        assert owner.close(timeout=3)


def test_transient_retry_anchors_include_failed_call_and_control_wait(monkeypatch):
    monkeypatch.setattr('lib.tasks_pkg.persistence_wait._BASE_DELAY_SECONDS', .001)
    owner = EventDeliveryCoordinator(workers=1, profile_every=1, idle_timeout=.01)
    lane = DeliveryLane({'id': 'retry'})
    calls, pushes = [], []
    def persist():
        calls.append(1)
        if len(calls) == 1:
            raise StorageError('database_busy', 'injected', retryable=True)
    try:
        owner.submit(lane, 1, 64, persist, lambda: pushes.append(1))
        owner.flush(lane)
        result = owner.snapshot()['performance_profile']
        assert len(calls) == 2 and pushes == [1]
        assert result['acceptedFrameAnchors'] == result['acknowledgedFrameAnchors'] == 1
        assert result['failedAttempts'] == 1
        assert result['attemptOutcomes']['database_busy']['count'] == result['attemptOutcomes']['ok']['count'] == 1
        assert result['stages']['attempt_gap']['count'] == 1
        assert result['stages']['attempt_gap']['totalNanos'] > 0
        assert result['stages']['persistence_wall']['totalNanos'] >= (result['stages']['storage_attempt']['totalNanos']
            + result['stages']['attempt_gap']['totalNanos'])
    finally:
        assert owner.close(timeout=3)

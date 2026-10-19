"""Opt-in FIFO/ACK/publication anchors for the existing delivery coordinator.

Entry: DeliveryProfiler; policy comes from the generated delivery contract.
Only anonymous batch numbers, event sequences and integer clocks are retained.
No callbacks, payloads, owners, durable state, workers or external I/O are owned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import sys
import threading
import time

from lib.agent_core.generated_task_event_delivery_profile import DELIVERY_PROFILE_POLICY


@dataclass(eq=False)
class DeliverySample:
    number: int
    frames: list = field(default_factory=list)
    dispatch_ns: int | None = None
    ack_ns: int | None = None
    attempt_end_ns: int | None = None
    attempt_wall_ns: int = 0
    attempt_gap_ns: int = 0
    attempts: int = 0
    failed_attempts: int = 0
    published: int = 0
    publish_errors: int = 0
    max_publish_ns: int = 0


class DeliveryProfiler:
    def __init__(self, every, *, clock_ns=time.monotonic_ns):
        self.every = every
        self.clock_ns = clock_ns
        self.lock = threading.RLock()
        self.seen = self.selected = self.completed = self.invalid = self.observer_errors = 0
        self.failed_rounds = self.peak_frames = self.active_frames = self.barrier_errors = 0
        self.accepted_frames = self.acknowledged_frames = self.failed_attempts = self.publish_errors = 0
        self.active = {}
        self.slow = []
        self.stages = {name: dict(count=0, totalNanos=0, maxNanos=0,
            log2NanosHistogram=[0] * 64) for name in DELIVERY_PROFILE_POLICY['anchors']}
        self.outcomes = dict(committed=0, revoked=0)
        self.attempt_outcomes = {name: dict(count=0, totalNanos=0, maxNanos=0)
            for name in DELIVERY_PROFILE_POLICY['attemptOutcomes']}

    def observe(self, method, *args):
        # Instrumentation failure must never turn an ACK into a storage retry.
        try:
            return getattr(self, method)(*args)
        except Exception:
            with self.lock:
                self.observer_errors += 1
            return None

    def accept(self, sequence):
        with self.lock:
            self.seen += 1
            if (self.seen - 1) % self.every:
                return None
            accepted = self.clock_ns()
            self.selected += 1
            sample = DeliverySample(self.seen, frames=[(sequence, accepted)])
            self.active[sample.number] = sample
            self.active_frames += 1
            self.accepted_frames += 1
            self.peak_frames = max(self.peak_frames, self.active_frames)
            return sample

    def accept_frame(self, sample, sequence):
        now = self.clock_ns()
        with self.lock:
            sample.frames.append((sequence, now))
            self.active_frames += 1
            self.accepted_frames += 1
            self.peak_frames = max(self.peak_frames, self.active_frames)

    def _record(self, stage, nanos):
        # An invalid clock never becomes a plausible zero-latency observation.
        if nanos < 0:
            self.invalid += 1
            return
        row = self.stages[stage]
        row['count'] += 1
        row['totalNanos'] += nanos
        row['maxNanos'] = max(row['maxNanos'], nanos)
        row['log2NanosHistogram'][min(63, max(0, nanos - 1).bit_length())] += 1

    def dispatch(self, sample):
        now = self.clock_ns()
        with self.lock:
            if sample.dispatch_ns is None:
                sample.dispatch_ns = now
                for _, accepted in sample.frames:
                    self._record('fifo_wait', now - accepted)

    def attempt_start(self, sample):
        now = self.clock_ns()
        with self.lock:
            if sample.attempt_end_ns is not None:
                gap = now - sample.attempt_end_ns
                self._record('attempt_gap', gap)
                sample.attempt_gap_ns += max(0, gap)
        return now

    def failure_outcome(self, error):
        code = getattr(error, 'code', None)
        return code if code in DELIVERY_PROFILE_POLICY['attemptOutcomes'] and code != 'ok' else 'other_failure'

    def attempt_end(self, sample, start, failed, outcome=None):
        now = self.clock_ns()
        with self.lock:
            duration = now - start
            self._record('storage_attempt', duration)
            outcome = ('other_failure' if failed else 'ok') if outcome is None else outcome
            if outcome not in self.attempt_outcomes:
                outcome = 'other_failure'
            if duration >= 0:
                row = self.attempt_outcomes[outcome]
                row['count'] += 1
                row['totalNanos'] += duration
                row['maxNanos'] = max(row['maxNanos'], duration)
            sample.attempt_wall_ns += max(0, now - start)
            sample.attempt_end_ns = now
            sample.attempts += 1
            sample.failed_attempts += int(failed)
            self.failed_attempts += int(failed)

    def acknowledge(self, sample):
        now = self.clock_ns()
        with self.lock:
            sample.ack_ns = now
            self.acknowledged_frames += len(sample.frames)
            self._record('persistence_wall', now - sample.dispatch_ns)

    def publication(self, sample, sequence, visibility_start, callback_start, end, failed):
        with self.lock:
            accepted = next((t for seq, t in sample.frames if seq == sequence), None)
            if accepted is None or sample.ack_ns is None:
                self.invalid += 1
                return
            self._record('ack_to_visibility', visibility_start - sample.ack_ns)
            self._record('visibility_update', (callback_start if callback_start is not None else end) - visibility_start)
            if callback_start is not None:
                self._record('publish_callback', end - callback_start)
                self._record('accept_to_publish', end - accepted)
            sample.max_publish_ns = max(sample.max_publish_ns, end - accepted)
            sample.published += int(callback_start is not None)
            sample.publish_errors += int(failed)
            self.publish_errors += int(failed)

    def failed_round(self):
        with self.lock:
            self.failed_rounds += 1

    def finish(self, sample, outcome):
        with self.lock:
            self.active.pop(sample.number, None)
            self.active_frames -= len(sample.frames)
            self.completed += 1
            self.outcomes[outcome] += 1
            row = dict(sample=sample.number, events=len(sample.frames), attempts=sample.attempts,
                failedAttempts=sample.failed_attempts, enteredPublishCallbacks=sample.published,
                publishErrors=sample.publish_errors, outcome=outcome,
                attemptWallNanos=sample.attempt_wall_ns, attemptGapNanos=sample.attempt_gap_ns,
                maxAcceptToPublishNanos=sample.max_publish_ns,
                acceptedToAckNanos=None if sample.ack_ns is None else sample.ack_ns - sample.frames[0][1])
            self.slow.append(row)
            self.slow.sort(key=lambda r: max(r['maxAcceptToPublishNanos'], r['acceptedToAckNanos'] or 0), reverse=True)
            del self.slow[DELIVERY_PROFILE_POLICY['maximumSlowSamples']:]

    def barrier(self, start, failed):
        end = self.clock_ns()
        with self.lock:
            self._record('flush_wall', end - start)
            self.barrier_errors += int(failed)

    def snapshot(self):
        now = self.clock_ns()
        with self.lock:
            oldest = sorted(self.active.values(), key=lambda s: s.frames[0][1])[:DELIVERY_PROFILE_POLICY['maximumSlowSamples']]
            # Snapshot copies metadata only. It never walks user graphs.
            return dict(enabled=True, contract=DELIVERY_PROFILE_POLICY['format'],
                sampleEvery=self.every, seenBatches=self.seen, selectedBatches=self.selected,
                completedSamples=self.completed, activeSamples=len(self.active),
                acceptedFrameAnchors=self.accepted_frames, acknowledgedFrameAnchors=self.acknowledged_frames,
                failedAttempts=self.failed_attempts, publishErrors=self.publish_errors,
                activeFrameAnchors=self.active_frames,
                peakFrameAnchors=self.peak_frames, invalidClocks=self.invalid, observerErrors=self.observer_errors,
                failedPersistenceRounds=self.failed_rounds, barrierErrors=self.barrier_errors, outcomes=dict(self.outcomes),
                policy=DELIVERY_PROFILE_POLICY,
                attemptOutcomes={name: dict(row) for name, row in self.attempt_outcomes.items()},
                stages={name: {**row, 'log2NanosHistogram': list(row['log2NanosHistogram'])}
                    for name, row in self.stages.items()},
                slowSamples=[dict(row) for row in self.slow],
                oldestActive=[dict(sample=s.number, events=len(s.frames), attempts=s.attempts,
                    ageNanos=now - s.frames[0][1], persistenceStarted=s.dispatch_ns is not None)
                    for s in oldest],
                metadataBytes=_metadata_bytes((self.active, self.slow, self.stages, self.outcomes, self.attempt_outcomes)))


def _metadata_bytes(value, seen=None):
    """Observed Python shallow allocations; excludes locks/allocator overhead."""
    seen = set() if seen is None else seen
    if id(value) in seen:
        return 0
    seen.add(id(value))
    total = sys.getsizeof(value)
    if isinstance(value, DeliverySample):
        return total + _metadata_bytes(vars(value), seen)
    if isinstance(value, dict):
        return total + sum(_metadata_bytes(k, seen) + _metadata_bytes(v, seen) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return total + sum(_metadata_bytes(v, seen) for v in value)
    return total

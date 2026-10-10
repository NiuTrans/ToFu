"""Task-local execution timestamps, independent of observers and wall-clock jumps.

HTTP adapters explicitly carry ingress clocks; TaskRuntime otherwise creates
one at acceptance. Producers capture its anchored
monotonic time before persistence/fan-out; storage retains that timestamp in
the event metadata spine. Legacy/recovered carriers without a live clock use
wall time and cannot claim monotonic evidence.
"""

from dataclasses import dataclass
import time
import uuid


@dataclass(frozen=True, slots=True)
class ExecutionClock:
    origin_at_ms: int
    origin_monotonic_ns: int
    clock_id: str

    @classmethod
    def start(cls, *, origin_at_ms=None, origin_monotonic_ns=None):
        if origin_monotonic_ns is not None and origin_at_ms is None:
            raise ValueError('A captured monotonic origin requires its epoch anchor')
        monotonic_ns = (time.monotonic_ns() if origin_monotonic_ns is None
                        else int(origin_monotonic_ns))
        epoch_ms = int(time.time() * 1000) if origin_at_ms is None else int(origin_at_ms)
        return cls(epoch_ms, monotonic_ns, uuid.uuid4().hex)

    def now_ms(self):
        return self.origin_at_ms + max(
            0, (time.monotonic_ns() - self.origin_monotonic_ns) // 1_000_000)


def task_execution_time_ms(task):
    clock = (task or {}).get('_executionClock')
    return clock.now_ms() if isinstance(clock, ExecutionClock) else int(time.time() * 1000)


def stamp_execution_clock(task, event):
    """Capture a local producer once; never re-stamp a relayed child frame."""
    clock = task.get('_executionClock')
    if not isinstance(clock, ExecutionClock):
        return
    if event.get('taskId') not in (None, '', task.get('id')):
        return
    if event.get('attemptId') not in (None, '', task.get('_attemptId')):
        return
    if 'executionAtMs' in event:
        return
    event['executionAtMs'] = clock.now_ms()
    # Token rows need only the timestamp spine. Structural boundaries prove
    # clock provenance without repeating two immutable fields per token.
    if event.get('type') != 'delta':
        event['executionOriginAtMs'] = clock.origin_at_ms
        event['executionClockId'] = clock.clock_id

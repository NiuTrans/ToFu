"""Process-local weighted admission for serialized storage frame bodies.

``FrameByteAdmission`` is the sole concurrency primitive shared by storage
clients and the Sidecar server. It owns no sockets or deployment policy; its
callers supply the process-specific byte capacity and bounded wait.
"""

from __future__ import annotations

from collections import deque
import threading
import time
from typing import Callable


class FrameByteAdmission:
    """Response-priority FIFO budget for serialized frame bodies."""

    def __init__(
        self,
        *,
        capacity_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity_bytes = max(1, int(capacity_bytes))
        self._clock = clock
        self._condition = threading.Condition()
        self._waiters: deque[tuple[object, int]] = deque()
        self._response_waiters: deque[tuple[object, int]] = deque()
        self._inflight_bytes = 0
        self._peak_bytes = 0
        self._waits = 0
        self._rejections = 0
        self._admitted_bytes_total = 0
        self._request_bytes_total = 0
        self._request_bytes_max = 0
        self._response_bytes_total = 0
        self._response_bytes_max = 0

    def _admit_unlocked(self, size: int) -> None:
        self._inflight_bytes += size
        self._peak_bytes = max(self._peak_bytes, self._inflight_bytes)
        self._admitted_bytes_total += size

    def acquire(
        self,
        size: int,
        *,
        timeout_s: float,
        response_priority: bool = False,
    ) -> bool:
        """Reserve bytes; completed responses drain before request FIFO."""
        size = max(1, int(size))
        timeout_s = max(0.0, float(timeout_s))
        deadline = self._clock() + timeout_s
        with self._condition:
            if (not self._response_waiters and not self._waiters
                    and self._inflight_bytes + size <= self.capacity_bytes):
                self._admit_unlocked(size)
                return True
            self._waits += 1
            ticket = object()
            waiter = (ticket, size)
            queue = self._response_waiters if response_priority else self._waiters
            queue.append(waiter)
            while True:
                is_head = (
                    queue[0][0] is ticket
                    and (response_priority or not self._response_waiters)
                )
                if (is_head
                        and self._inflight_bytes + size
                        <= self.capacity_bytes):
                    queue.popleft()
                    self._admit_unlocked(size)
                    self._condition.notify_all()
                    return True
                remaining = deadline - self._clock()
                if remaining <= 0:
                    was_head = queue[0][0] is ticket
                    queue.remove(waiter)
                    self._rejections += 1
                    if was_head:
                        self._condition.notify_all()
                    return False
                self._condition.wait(remaining)

    def release(self, size: int) -> None:
        size = int(size)
        with self._condition:
            if size <= 0 or size > self._inflight_bytes:
                raise RuntimeError('invalid storage frame-byte release')
            self._inflight_bytes -= size
            self._condition.notify_all()

    def observe_frame(self, direction: str, size: int) -> None:
        size = max(0, int(size))
        with self._condition:
            if direction == 'request':
                self._request_bytes_total += size
                self._request_bytes_max = max(self._request_bytes_max, size)
            elif direction == 'response':
                self._response_bytes_total += size
                self._response_bytes_max = max(self._response_bytes_max, size)
            else:
                raise ValueError('frame direction must be request or response')

    def metrics(self) -> dict[str, int]:
        with self._condition:
            return {
                'frame_bytes_inflight': self._inflight_bytes,
                'frame_bytes_capacity': self.capacity_bytes,
                'frame_bytes_peak': self._peak_bytes,
                'frame_admission_waiting': (
                    len(self._response_waiters) + len(self._waiters)),
                'frame_admission_waits': self._waits,
                'frame_admission_rejections': self._rejections,
                'frame_bytes_admitted_total': self._admitted_bytes_total,
                'request_frame_bytes_total': self._request_bytes_total,
                'request_frame_bytes_max': self._request_bytes_max,
                'response_frame_bytes_total': self._response_bytes_total,
                'response_frame_bytes_max': self._response_bytes_max,
            }


__all__ = ['FrameByteAdmission']

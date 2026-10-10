"""Process-local memory/file ownership for serialized storage frame bodies.

``FrameByteAdmission`` is the shared buffer ownership policy used by storage
clients and the Sidecar server. It owns no sockets or deployment policy; its
callers supply the RAM threshold; excess receiving bodies use anonymous files.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class FrameBufferReservation:
    """One exact buffer representation claim; close releases it once."""
    def __init__(self, owner, size: int, spilled: bool, chunk_bytes: int):
        self.owner = owner
        self.size = size
        self.spilled = spilled
        self.chunk_bytes = chunk_bytes
        self._closed = False

    def close(self) -> None:
        with self.owner._condition:
            if self._closed:
                return
            self._closed = True
            self.owner._inflight_bytes -= self.size
            if self.spilled:
                self.owner._file_bytes -= self.size
            else:
                self.owner._memory_bytes -= self.size


class FrameByteAdmission:
    """Memory buffer threshold with immediate anonymous-file overflow."""

    def __init__(
        self,
        *,
        capacity_bytes: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.capacity_bytes = max(1, int(capacity_bytes))
        self._clock = clock
        self._condition = threading.Condition()
        self._memory_bytes = 0
        self._file_bytes = 0
        self._spill_bytes_total = 0
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

    def reserve_frame(self, size: int) -> FrameBufferReservation:
        """Choose RAM or disk immediately; work never waits for a byte slot."""
        size = max(1, int(size))
        with self._condition:
            spilled = self._memory_bytes + size > self.capacity_bytes
            self._admit_unlocked(size)
            if spilled:
                self._file_bytes += size
                self._spill_bytes_total += size
            else:
                self._memory_bytes += size
            chunk_bytes = min(size, max(64 * 1024, min(self.capacity_bytes // 4, 1024 * 1024)))
            return FrameBufferReservation(self, size, spilled, chunk_bytes)

    def acquire(self, size: int, *, timeout_s: float,
                response_priority: bool = False) -> bool:
        """Account an already-owned buffer without queueing or refusal.

        Receiving callers use reserve_frame for exact representation ownership.
        Legacy/write callers already own their encoded bytes; this compatibility
        ledger charges them until release without manufacturing a pressure error.
        """
        size = max(1, int(size))
        with self._condition:
            self._admit_unlocked(size)
            self._memory_bytes += size
        return True

    def release(self, size: int) -> None:
        size = int(size)
        with self._condition:
            if size <= 0 or size > self._inflight_bytes or size > self._memory_bytes:
                raise RuntimeError('invalid storage frame-byte release')
            self._inflight_bytes -= size
            self._memory_bytes -= size
            self._condition.notify_all()

    def resize(self, capacity_bytes: int) -> None:
        """Change future RAM choices; existing buffer owners retain their leases."""
        if type(capacity_bytes) is not int or capacity_bytes < 1:
            raise ValueError('frame capacity must be positive')
        with self._condition:
            self.capacity_bytes = int(capacity_bytes)
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
                'frame_admission_waiting': 0,
                'frame_buffer_memory_bytes': self._memory_bytes,
                'frame_buffer_file_bytes': self._file_bytes,
                'frame_buffer_spill_bytes_total': self._spill_bytes_total,
                'frame_admission_waits': self._waits,
                'frame_admission_rejections': self._rejections,
                'frame_bytes_admitted_total': self._admitted_bytes_total,
                'request_frame_bytes_total': self._request_bytes_total,
                'request_frame_bytes_max': self._request_bytes_max,
                'response_frame_bytes_total': self._response_bytes_total,
                'response_frame_bytes_max': self._response_bytes_max,
            }


def process_response_frame_admission() -> FrameByteAdmission:
    """Return the one serialized-response budget for this client process.

    Shared by the storage.v1 client (whole-frame admission) and the
    storage.v2 client (per-frame admission across the chunk stream) so the
    process worst case is one budget — not one per protocol. The singleton
    state stays in ``lib/storage/client.py`` (its established owner — tests
    reset it there); this is the stable shared accessor both clients use.
    Tests substitute their own RAM-threshold instance through each client's
    constructor; they never mutate the process global.
    """
    from lib.storage.client import _process_response_frame_admission

    return _process_response_frame_admission()


__all__ = ['FrameByteAdmission', 'process_response_frame_admission']

"""Advancing lease time owned by one backend lifecycle.

Entry point: WorkerLeaseClock.now_ms. Adapters share this object and their existing
volatile event-read epoch with every session. No caller timestamp, environment
override, thread, or durable clock row participates in lease authority.
"""
from threading import Lock
import time


class WorkerLeaseClock:
    def __init__(self, *, wall_ns=time.time_ns, monotonic_ns=time.monotonic_ns):
        self._wall_ns = wall_ns
        self._monotonic_ns = monotonic_ns
        self._offset_ms = wall_ns() // 1_000_000
        self._origin_ns = monotonic_ns()
        if self._offset_ms <= 0:
            raise ValueError('Worker lease authority requires a positive UTC clock')
        self._lock = Lock()

    def snapshot(self) -> tuple[int, int]:
        with self._lock:
            # UTC first keeps a pause between probes from inflating the offset.
            wall = self._wall_ns() // 1_000_000
            elapsed = (self._monotonic_ns() - self._origin_ns) // 1_000_000
            if elapsed < 0 or wall <= 0:
                raise ValueError('Worker lease authority clock is invalid')
            self._offset_ms = max(self._offset_ms, wall - elapsed)
            return self._offset_ms + elapsed, wall

    def now_ms(self) -> int:
        return self.snapshot()[0]

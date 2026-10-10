"""Real-clock waits for public worker-lease and queue contracts.

Production RPCs intentionally have no test-clock override. Unit tests inject a
clock at the backend dependency; public contracts wait for actual eligibility.
"""
import time


def wall_ms() -> int:
    return time.time_ns() // 1_000_000


def wait_until_ms(deadline: int) -> None:
    remaining = deadline - wall_ms()
    assert remaining <= 60_000, 'Worker contract wait exceeds its test bound'
    limit = time.monotonic() + max(remaining, 0) / 1000 + 2
    while wall_ms() < deadline:
        assert time.monotonic() < limit, 'Worker contract clock did not advance'
        time.sleep(min(0.25, max(0.001, (deadline - wall_ms()) / 1000)))

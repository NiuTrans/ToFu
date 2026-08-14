"""Bounded PG-availability wait before the TOFU_REQUIRE_PG fail-closed gate.

Root cause pinned here: on a self-triggered re-exec (in-app Restart / update),
the app-owned userspace postmaster is torn down with the old process and comes
back within seconds. A single 5s connect probe that then aborts fail-closed
(``_assert_pg_available_or_raise``) turns that transient race into a hard boot
failure. The fix waits for PG — retrying the full bootstrap (probe + start)
with exponential backoff — until a bounded deadline before refusing.

Two pure seams are pinned:
  * ``_retry_until`` — the generic bounded-retry-with-backoff primitive.
  * ``_pg_require_wait_s`` — deadline is 0 unless TOFU_REQUIRE_PG is set, so a
    single-box / PG-optional boot adds ZERO latency (byte-identical).

Import is safe under the test harness because conftest forces
TOFU_DB_BACKEND=sqlite, so importing _core skips the module-load PG bootstrap.
"""

import pytest

_core = pytest.importorskip('lib.database._core')

pytestmark = pytest.mark.unit


class _Clock:
    """Deterministic monotonic + sleep pair: sleeping advances the clock."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


# ── _retry_until ────────────────────────────────────────────────────────

def test_deadline_zero_is_exactly_one_attempt_no_sleep():
    """deadline_s<=0 → single attempt, never sleeps (single-box byte-identity)."""
    clk = _Clock()
    calls = []

    def attempt():
        calls.append(1)
        return None  # always fails

    out = _core._retry_until(attempt, deadline_s=0, sleep=clk.sleep,
                             monotonic=clk.monotonic)
    assert out is None
    assert len(calls) == 1          # exactly one shot
    assert clk.sleeps == []         # never waited


def test_deadline_zero_returns_success_immediately():
    clk = _Clock()
    out = _core._retry_until(lambda: 'PG', deadline_s=0, sleep=clk.sleep,
                             monotonic=clk.monotonic)
    assert out == 'PG'
    assert clk.sleeps == []


def test_transient_failure_then_success_within_deadline():
    """The re-exec race: PG unavailable for the first N attempts, then up.
    Must return the success result instead of giving up."""
    clk = _Clock()
    seq = [None, None, 'PG_UP']
    calls = []

    def attempt():
        calls.append(1)
        return seq[len(calls) - 1]

    out = _core._retry_until(attempt, deadline_s=60, sleep=clk.sleep,
                             monotonic=clk.monotonic, base_backoff_s=1.0)
    assert out == 'PG_UP'
    assert len(calls) == 3
    assert clk.sleeps == [1.0, 2.0]     # exponential backoff between tries


def test_never_available_gives_up_at_deadline_and_is_bounded():
    """Never comes up → returns None, total wait never exceeds the deadline."""
    clk = _Clock()
    calls = []

    def attempt():
        calls.append(1)
        return None

    out = _core._retry_until(attempt, deadline_s=10, sleep=clk.sleep,
                             monotonic=clk.monotonic, base_backoff_s=1.0,
                             max_backoff_s=8.0)
    assert out is None
    assert sum(clk.sleeps) <= 10 + 1e-9      # bounded by the deadline
    assert clk.monotonic() <= 10 + 1e-9
    # final sleep clamped so we don't overshoot
    assert all(s >= 0 for s in clk.sleeps)


def test_backoff_is_clamped_to_not_overshoot_deadline():
    clk = _Clock()
    out = _core._retry_until(lambda: None, deadline_s=5, sleep=clk.sleep,
                             monotonic=clk.monotonic, base_backoff_s=4.0,
                             max_backoff_s=100.0)
    assert out is None
    assert sum(clk.sleeps) <= 5 + 1e-9


# ── _pg_require_wait_s ────────────────────────────────────────────────────

def test_wait_is_zero_when_pg_not_required(monkeypatch):
    monkeypatch.delenv('TOFU_REQUIRE_PG', raising=False)
    assert _core._pg_require_wait_s() == 0.0


def test_wait_defaults_to_60_when_pg_required(monkeypatch):
    monkeypatch.setenv('TOFU_DB_BACKEND', 'postgres')
    monkeypatch.delenv('TOFU_PG_REQUIRE_WAIT_S', raising=False)
    assert _core._pg_require_wait_s() == 60.0


def test_wait_is_env_tunable_when_pg_required(monkeypatch):
    monkeypatch.setenv('TOFU_DB_BACKEND', 'postgres')
    monkeypatch.setenv('TOFU_PG_REQUIRE_WAIT_S', '25')
    assert _core._pg_require_wait_s() == 25.0


def test_wait_invalid_env_falls_back_to_60(monkeypatch):
    monkeypatch.setenv('TOFU_DB_BACKEND', 'postgres')
    monkeypatch.setenv('TOFU_PG_REQUIRE_WAIT_S', 'not-a-number')
    assert _core._pg_require_wait_s() == 60.0


if __name__ == '__main__':
    import sys
    sys.exit(pytest.main([__file__, '-v']))

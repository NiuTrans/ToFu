"""tests/scheduler/test_scheduler_timer_lease.py — per-timer watcher lease contracts.

Covers the distributed single-owner guarantee for timer watchers:

  1. Two replicas racing ``claim_watcher_lease`` → exactly one winner.
  2. A watcher that loses its lease mid-run stops cooperatively (registry
     cleaned, foreign claim untouched, no DB writes needed).
  3. A foreign-held lease blocks a local ``start_timer_loop``.
  4. Release tombstones the claim so a standby can adopt quickly.
  5. Personal mode (default inproc store): a fresh claim always succeeds and
     ``start_timer_loop`` behaves exactly as before the lease existed.
"""

from __future__ import annotations

import time
import uuid

import pytest

pytestmark = pytest.mark.unit


def _tid() -> str:
    return f'timer-lease-{uuid.uuid4().hex[:12]}'


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    from lib.scheduler.timer import _loop, _runtime
    runtime = _runtime.TimerWatcherRuntime()
    monkeypatch.setattr(_runtime, '_runtime', runtime)
    with _loop._timers_lock:
        _loop._active_timers.clear()
    yield
    runtime.shutdown()
    with _loop._timers_lock:
        _loop._active_timers.clear()


def _fake_timer(timer_id):
    return {
        'id': timer_id,
        'user_id': 1,
        'conv_id': 'conv-lease-test',
        'status': 'active',
        'poll_interval': 0.1,
        'max_polls': 100,
        'poll_count': 0,
    }


# ── 1. racing claims → exactly one winner ────────────────────────────────────

def test_racing_claims_exactly_one_winner():
    from lib.scheduler.timer import _lease

    tid = _tid()
    token_a = _lease.claim_watcher_lease(tid)
    token_b = _lease.claim_watcher_lease(tid)

    assert token_a is not None
    assert token_b is None, 'second claim on a live lease must lose'
    assert token_a != token_b


def test_claims_on_distinct_timers_do_not_interfere():
    from lib.scheduler.timer import _lease

    assert _lease.claim_watcher_lease(_tid()) is not None
    assert _lease.claim_watcher_lease(_tid()) is not None


# ── 2. lease loss stops the local watcher cooperatively ─────────────────────

def test_lease_loss_stops_local_watcher(monkeypatch):
    from lib.runtime.runtime_state_store import get_store
    from lib.scheduler.timer import _lease, _loop

    tid = _tid()
    monkeypatch.setattr(
        _loop, '_get_timer_row', lambda timer_id, *, user_id: _fake_timer(timer_id))
    monkeypatch.setattr(
        _loop, 'poll_timer',
        lambda *a, **k: (False, 'wait', 0, False, False, '', 'm', None, ''))
    monkeypatch.setattr(
        _loop, '_record_poll', lambda *a, **k: None)

    assert _loop.start_timer_loop(tid, user_id=1) is True
    with _loop._timers_lock:
        thread = _loop._active_timers[tid]

    # Another replica takes over: overwrite the claim with a foreign token.
    get_store().set_value(_lease.WATCHER_LEASE_KIND, tid, 'foreign-owner', 120.0)

    thread.join(timeout=10)
    assert not thread.is_alive(), 'watcher must stop after losing its lease'
    with _loop._timers_lock:
        assert tid not in _loop._active_timers
    # Cooperative stop: the foreign claim is NOT clobbered.
    assert get_store().get_value(_lease.WATCHER_LEASE_KIND, tid) == 'foreign-owner'


# ── 3. foreign-held lease blocks a local start ───────────────────────────────

def test_foreign_lease_blocks_local_start(monkeypatch):
    from lib.runtime.runtime_state_store import get_store
    from lib.scheduler.timer import _lease, _loop

    tid = _tid()
    get_store().set_value(_lease.WATCHER_LEASE_KIND, tid, 'foreign-owner', 120.0)

    class _NoThread:
        def __init__(self, *a, **k):
            self.started = False

        def start(self):
            self.started = True  # pragma: no cover - must never run

    monkeypatch.setattr(_loop, 'TimerWatcherHandle', _NoThread)
    monkeypatch.setattr(
        _loop, '_get_timer_row', lambda timer_id, *, user_id: _fake_timer(timer_id))

    assert _loop.start_timer_loop(tid, user_id=1) is False
    with _loop._timers_lock:
        assert tid not in _loop._active_timers


# ── 4. release lets a standby adopt quickly ─────────────────────────────────

def test_release_allows_prompt_reclaim():
    from lib.scheduler.timer import _lease

    tid = _tid()
    token = _lease.claim_watcher_lease(tid)
    assert token is not None

    _lease.release_watcher_lease(tid, 'not-the-owner')
    # A non-owner release is a no-op: the original claim still blocks.
    assert _lease.claim_watcher_lease(tid) is None

    _lease.release_watcher_lease(tid, token)
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _lease.claim_watcher_lease(tid) is not None:
            break
        time.sleep(0.05)
    else:
        pytest.fail('released lease must become claimable (tombstone expiry)')


def test_renew_detects_takeover():
    from lib.runtime.runtime_state_store import get_store
    from lib.scheduler.timer import _lease

    tid = _tid()
    token = _lease.claim_watcher_lease(tid)
    assert token is not None
    assert _lease.renew_watcher_lease(tid, token) is True

    get_store().set_value(_lease.WATCHER_LEASE_KIND, tid, 'foreign-owner', 120.0)
    assert _lease.renew_watcher_lease(tid, token) is False


# ── 5. personal mode: fresh claim always succeeds, start path unchanged ──────

def test_personal_mode_start_unchanged(monkeypatch):
    from lib.scheduler.timer import _loop

    class _FakeThread:
        def __init__(self, *a, **k):
            self.started = False

        def start(self):
            self.started = True

    tid = _tid()
    monkeypatch.setattr(_loop, 'TimerWatcherHandle', _FakeThread)
    monkeypatch.setattr(
        _loop, '_get_timer_row', lambda timer_id, *, user_id: _fake_timer(timer_id))

    assert _loop.start_timer_loop(tid, user_id=1) is True
    with _loop._timers_lock:
        assert tid in _loop._active_timers

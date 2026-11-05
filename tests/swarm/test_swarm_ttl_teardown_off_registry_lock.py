"""TTL teardown never runs storage I/O under the swarm session registry lock.

The registry lock serves every session lookup: Stop (cancel_task), await_agents,
poll/kill, status routes and spawns. A TTL sweep used to clear the expired
session's inbox, delete its durable rows and abort it while holding that lock,
so one slow storage call stalled every unrelated swarm operation. Teardown now
runs on its own worker after the session leaves the registry. Only a
re-registration of the same key waits for it, because a late delete would wipe
the durable rows the new session persists right after registering.
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

import lib.swarm.integration as integration
from lib.swarm.integration import _state


pytestmark = pytest.mark.unit

STALE_KEY = 'ttl-teardown-stale-conv'


class _ExpiredSession:
    is_terminated = True
    user_id = 1
    progress_beacon = None

    def __init__(self):
        self.aborted = threading.Event()

    def record_run_results(self):
        return True

    def abort(self):
        self.aborted.set()


@pytest.fixture
def blocked_teardown(monkeypatch):
    """One expired session whose durable delete blocks until released."""
    entered = threading.Event()
    release = threading.Event()
    deleted = []

    def slow_delete(key):
        entered.set()
        release.wait(5)
        deleted.append(key)

    monkeypatch.setattr('lib.swarm.persistence.delete_session', slow_delete)
    monkeypatch.setattr(_state.agent_inbox, 'clear', lambda *_args, **_kwargs: None)
    session = _ExpiredSession()
    with _state._sessions_lock:
        _state._active_sessions[STALE_KEY] = session
        _state._session_timestamps[STALE_KEY] = (
            time.time() - 10 * _state.SESSION_TTL_SECONDS)
    monkeypatch.setattr(integration, '_last_cleanup', 0.0)
    monkeypatch.setattr(_state, '_last_cleanup', 0.0)
    try:
        yield SimpleNamespace(
            entered=entered, release=release, deleted=deleted, session=session)
    finally:
        release.set()
        assert _state.stop_swarm_cleanup_timer(timeout=5.0)
        with _state._sessions_lock:
            _state._active_sessions.pop(STALE_KEY, None)
            _state._session_timestamps.pop(STALE_KEY, None)


def _sweep_in_background():
    sweeper = threading.Thread(
        target=integration.get_active_session, args=('any-task',), daemon=True)
    sweeper.start()
    return sweeper


def test_unrelated_lookups_never_wait_for_ttl_teardown_io(blocked_teardown):
    sweeper = _sweep_in_background()
    assert blocked_teardown.entered.wait(5), 'the sweep never reached the durable delete'

    started = time.monotonic()
    assert integration.get_active_session('another-conversation') is None
    assert time.monotonic() - started < 1.0

    sweeper.join(1.0)
    assert not sweeper.is_alive(), 'the sweeping caller waited for teardown I/O'
    assert STALE_KEY not in integration._active_sessions
    blocked_teardown.release.set()
    assert blocked_teardown.session.aborted.wait(5)
    assert blocked_teardown.deleted == [STALE_KEY]


def test_reregistering_the_same_key_waits_for_its_teardown(blocked_teardown):
    sweeper = _sweep_in_background()
    assert blocked_teardown.entered.wait(5)
    replacement = SimpleNamespace(is_terminated=False, user_id=1, progress_beacon=None)
    registered = threading.Event()

    def register():
        _state._set_session(STALE_KEY, replacement)
        registered.set()

    registrar = threading.Thread(target=register, daemon=True)
    registrar.start()
    assert not registered.wait(0.3)
    assert STALE_KEY not in integration._active_sessions

    blocked_teardown.release.set()
    assert registered.wait(5)
    assert blocked_teardown.deleted == [STALE_KEY]
    assert integration._active_sessions[STALE_KEY] is replacement
    registrar.join(5)
    sweeper.join(5)


def test_shutdown_joins_in_flight_ttl_teardown(blocked_teardown):
    sweeper = _sweep_in_background()
    assert blocked_teardown.entered.wait(5)

    assert integration.stop_swarm_cleanup_timer(timeout=0.2) is False
    blocked_teardown.release.set()
    assert integration.stop_swarm_cleanup_timer(timeout=5.0) is True
    assert blocked_teardown.session.aborted.is_set()
    sweeper.join(5)

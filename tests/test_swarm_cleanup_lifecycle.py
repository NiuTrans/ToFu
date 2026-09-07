"""Resource lifecycle contract for the process-wide swarm session registry."""

from __future__ import annotations

import threading

import pytest

import lib.swarm.integration._state as state


pytestmark = pytest.mark.unit
_TIMER_OBSERVED_AFTER_IMPORT = state._cleanup_timer


def test_import_does_not_start_cleanup_thread():
    assert _TIMER_OBSERVED_AFTER_IMPORT is None
    assert state.swarm_cleanup_snapshot()['timerAlive'] is False


def test_cleanup_timer_is_shared_retired_and_generation_safe(monkeypatch):
    import lib.swarm.integration as facade
    import lib.swarm.persistence as persistence

    state.stop_swarm_cleanup_timer(timeout=0.5)
    monkeypatch.setattr(state, '_CLEANUP_INTERVAL', 60)
    monkeypatch.setattr(persistence, 'delete_session', lambda _key: None)
    monkeypatch.setattr(state.agent_inbox, 'clear', lambda _key: None)
    keys = ('cleanup-life-1', 'cleanup-life-2', 'cleanup-life-3')
    owned_timers = []

    try:
        state._set_session(keys[0], object())
        first = state._cleanup_timer
        assert first is not None and first.is_alive()
        owned_timers.append(first)
        assert first.name == 'swarm-session-cleanup'
        assert facade._cleanup_timer is first

        state._set_session(keys[1], object())
        assert state._cleanup_timer is first

        # Exercise the production callback boundary without waiting 60s.
        first.cancel()
        first.function()
        first.join(timeout=0.5)
        second = state._cleanup_timer
        assert second is not None and second is not first
        owned_timers.append(second)
        assert second.is_alive()

        state._remove_session(keys[0])
        assert state._cleanup_timer is second

        # A canceled older generation must not detach or replace the owner.
        first.function()
        assert state._cleanup_timer is second

        state._remove_session(keys[1])
        second.join(timeout=0.5)
        assert not second.is_alive()
        assert state._cleanup_timer is None
        assert facade._cleanup_timer is None
        assert state.swarm_cleanup_snapshot()['activeSessions'] == 0

        state._set_session(keys[2], object())
        third = state._cleanup_timer
        assert third is not None and third is not second
        owned_timers.append(third)
        assert third.is_alive()
    finally:
        for key in keys:
            state._remove_session(key)
        assert state.stop_swarm_cleanup_timer(timeout=0.5)
        for timer in owned_timers:
            timer.join(timeout=0.5)
        assert not any(
            thread.name == 'swarm-session-cleanup' and thread.is_alive()
            for thread in threading.enumerate()
        )

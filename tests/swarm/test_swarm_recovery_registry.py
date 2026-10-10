"""Recovery registry admission is atomic, retryable, and bounded by unique keys."""

from __future__ import annotations

from contextlib import nullcontext
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import lib.swarm.integration as facade
from lib.swarm.integration import _state as state

pytestmark = pytest.mark.unit


@pytest.fixture
def registry(monkeypatch):
    """Own registry containers, clock, timers, and persistence without threads/DB."""
    for name, value in (
        ('_active_sessions', {}),
        ('_session_reservations', set()),
        ('_session_timestamps', {}),
        ('_key_aliases', {}),
        ('_sessions_lock', threading.Lock()),
        ('_autocontinue_chain', {}),
        ('_autocontinue_inflight', set()),
        ('_autocontinue_lock', threading.Lock()),
    ):
        monkeypatch.setattr(state, name, value)
        if hasattr(facade, name):
            monkeypatch.setattr(facade, name, value)
    for owner in (state, facade):
        monkeypatch.setattr(owner, '_cleanup_timer', None)
        monkeypatch.setattr(owner, '_last_cleanup', 1000.0)
        monkeypatch.setattr(owner, 'MAX_SESSIONS', 2)
    monkeypatch.setattr(state, '_cleanup_timer_starts', 0)
    monkeypatch.setattr(state, '_cleanup_timer_retirements', 0)
    controlled = SimpleNamespace(
        now=1000.0, timers=[], start_error=None, fail_after_start=False)
    monkeypatch.setattr(state, 'time', SimpleNamespace(time=lambda: controlled.now))

    class Timer:
        def __init__(self, interval, function):
            self.function = function
            self.alive = False
            self.cancelled = False
            controlled.timers.append(self)

        def start(self):
            if controlled.start_error is not None:
                self.alive = controlled.fail_after_start
                raise controlled.start_error
            self.alive = True

        def is_alive(self):
            return self.alive

        def cancel(self):
            self.cancelled = True
            self.alive = False

        def join(self, timeout=None):
            # No thread exists; cancellation must precede the bounded join.
            assert not self.alive

    monkeypatch.setattr(state, 'threading', SimpleNamespace(
        Timer=Timer, current_thread=threading.current_thread))
    saved = Mock()
    deleted = Mock()
    cleared = Mock()
    monkeypatch.setattr('lib.swarm.persistence.save_session', saved)
    monkeypatch.setattr('lib.swarm.persistence.delete_session', deleted)
    monkeypatch.setattr(state.agent_inbox, 'clear', cleared)
    try:
        yield controlled
    finally:
        assert facade.stop_swarm_cleanup_timer(timeout=0.1)
        assert not any(timer.is_alive() for timer in controlled.timers)
        saved.assert_not_called()
        deleted.assert_not_called()
        cleared.assert_not_called()


def _registry_entries():
    return (dict(facade._active_sessions), dict(facade._session_timestamps),
            dict(facade._key_aliases))


@pytest.mark.parametrize('preexisting', [False, True])
@pytest.mark.parametrize('fail_after_start', [False, True])
def test_timer_start_failure_rolls_back_reserved_registration_and_allows_retry(
        registry, preexisting, fail_after_start):
    if preexisting:
        other = SimpleNamespace(is_terminated=False)
        facade._set_session('other-conv', other, task_id='other-task')
        facade.add_session_alias('resume-task', 'other-conv')
        assert facade.stop_swarm_cleanup_timer(timeout=0.1)
    before = _registry_entries()
    session = SimpleNamespace(is_terminated=False)
    error = RuntimeError('injected Timer.start failure')
    registry.start_error = error
    registry.fail_after_start = fail_after_start
    with pytest.raises(RuntimeError, match='injected Timer.start failure') as caught:
        with state.reserve_session('recovering-conv'):
            facade._set_session('recovering-conv', session,
                                task_id='resume-task', reserved=True)
    assert caught.value is error
    assert _registry_entries() == before
    assert not state._session_reservations
    failed_timer = registry.timers[-1]
    assert failed_timer.cancelled and not failed_timer.is_alive()
    assert state._cleanup_timer is None
    assert facade._cleanup_timer is None

    registry.start_error = None
    with state.reserve_session('recovering-conv'):
        facade._set_session('recovering-conv', session,
                            task_id='resume-task', reserved=True)
    timer = state._cleanup_timer
    assert timer is not None and timer.is_alive()
    assert facade._cleanup_timer is timer
    assert facade.get_active_session('resume-task') is session
    assert not state._session_reservations
    if preexisting:
        assert facade.get_active_session('other-task') is other
    after = _registry_entries()
    failed_timer.function()
    assert state._cleanup_timer is timer
    assert facade._cleanup_timer is timer
    assert _registry_entries() == after


def test_timer_start_failure_restores_replaced_session_timestamp_and_aliases(registry):
    previous = SimpleNamespace(is_terminated=False)
    facade._set_session('recovering-conv', previous, task_id='original-task')
    assert facade.stop_swarm_cleanup_timer(timeout=0.1)
    before = _registry_entries()
    registry.now += 1.0
    registry.start_error = RuntimeError('replacement Timer.start failure')
    with pytest.raises(RuntimeError, match='replacement Timer.start failure'):
        facade._set_session('recovering-conv', SimpleNamespace(is_terminated=False),
                            task_id='resume-task')
    assert _registry_entries() == before
    assert facade._active_sessions['recovering-conv'] is previous
    assert 'resume-task' not in facade._key_aliases
    assert state._cleanup_timer is None
    assert facade._cleanup_timer is None
    assert registry.timers[-1].cancelled
    registry.start_error = None
    assert facade.get_active_session('original-task') is previous
    assert facade._cleanup_timer is state._cleanup_timer
    assert state._cleanup_timer.is_alive()


@pytest.mark.parametrize('second_reserved', [False, True])
def test_recovery_reservations_preserve_identity_without_session_capacity(registry, second_reserved):
    first = SimpleNamespace(is_terminated=False)
    second = SimpleNamespace(is_terminated=False)
    with state.reserve_session('first-conv'):
        facade._set_session('first-conv', first, task_id='first-task', reserved=True)
        timer = state._cleanup_timer
        with pytest.raises(state.SwarmSessionConflict, match='already admitted'):
            with state.reserve_session('first-conv'):
                pytest.fail('duplicate recovery reservation was admitted')
        with pytest.raises(state.SwarmSessionConflict, match='reserved'):
            facade._set_session('first-conv', object())
        admission = state.reserve_session('second-conv') if second_reserved else nullcontext()
        with admission:
            facade._set_session('second-conv', second,
                                task_id='second-task', reserved=second_reserved)
            assert facade.get_active_session('first-task') is first
            assert facade.get_active_session('second-task') is second
            assert len(facade._active_sessions) == 2
            assert state._cleanup_timer is timer
            with state.reserve_session('third-conv'):
                third = SimpleNamespace(is_terminated=False)
                facade._set_session('third-conv', third, task_id='third-task', reserved=True)
            assert facade._active_sessions['third-conv'] is third
            assert facade._key_aliases['third-task'] == 'third-conv'
    assert not state._session_reservations
    assert facade._active_sessions == {'first-conv': first, 'second-conv': second, 'third-conv': third}
    assert facade._cleanup_timer is timer

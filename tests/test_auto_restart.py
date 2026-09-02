"""Tests for the HEAD-moved auto-restart watcher (``lib/auto_restart.py``)
and the ``_perform_server_reexec`` extraction in ``routes/api_v1/update.py``.

The watcher is the automated half of the "effective" contract: a commit
only counts once the RUNNING process serves it. It must fire ONLY when
every precondition holds — env-gated, git checkout, HEAD actually moved,
shutdown not requested, zero in-flight tasks — and must DEFER (not drop)
a restart that arrives while busy. All seams are injected; no real execv.
"""
from __future__ import annotations

import threading

import pytest


@pytest.fixture
def state():
    return {'baseline_sha': None, 'restarting': False}


def _seams(**over):
    """Default all-green seam set; individual tests override one knob."""
    s = {
        'is_repo': lambda: True,
        'head_sha': lambda: 'aaa111',
        'running_tasks': lambda: [],
        'do_restart': lambda reason: True,
        'shutdown_requested': None,
    }
    s.update(over)
    return s


@pytest.mark.unit
def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv('TOFU_AUTO_RESTART', raising=False)
    from lib.auto_restart import maybe_start_auto_restart_watch
    assert maybe_start_auto_restart_watch() is False


@pytest.mark.unit
def test_enabled_starts_daemon_thread(monkeypatch):
    monkeypatch.setenv('TOFU_AUTO_RESTART', '1')
    import lib.auto_restart as ar
    started = []
    monkeypatch.setattr(ar, '_watch_thread', None)

    class _FakeThread:
        def __init__(self, target=None, kwargs=None, name=None, daemon=None):
            started.append({'target': target, 'kwargs': kwargs,
                            'name': name, 'daemon': daemon})

        def start(self):
            pass

        def is_alive(self):
            return True

    monkeypatch.setattr(ar.threading, 'Thread', _FakeThread)
    assert ar.maybe_start_auto_restart_watch(shutdown_requested='X') is True
    assert started and started[0]['daemon'] is True
    assert started[0]['name'] == 'tofu-auto-restart'
    assert started[0]['kwargs']['shutdown_requested'] == 'X'
    assert started[0]['kwargs']['stop_event'] is ar._watch_stop


@pytest.mark.unit
def test_stop_wakes_watcher_without_process_shutdown(monkeypatch):
    monkeypatch.setenv('TOFU_AUTO_RESTART', '1')
    import lib.auto_restart as ar

    monkeypatch.setattr(ar, '_watch_thread', None)
    monkeypatch.setattr(ar, 'poll_once', lambda *_args, **_kwargs: 'unchanged')
    shutdown_requested = threading.Event()

    assert ar.maybe_start_auto_restart_watch(
        shutdown_requested=shutdown_requested) is True
    thread = ar._watch_thread
    assert thread is not None and thread.is_alive()
    assert shutdown_requested.is_set() is False
    assert ar.stop_auto_restart_watch(timeout=0.5) is True
    assert not thread.is_alive()
    assert ar._watch_thread is None


@pytest.mark.unit
def test_first_poll_captures_baseline(state):
    from lib.auto_restart import poll_once
    v = poll_once(state, **_seams(head_sha=lambda: 'aaa111'))
    assert v == 'baseline'
    assert state['baseline_sha'] == 'aaa111'
    assert poll_once(state, **_seams(head_sha=lambda: 'aaa111')) == 'unchanged'


@pytest.mark.unit
def test_head_move_with_tasks_running_defers(state):
    from lib.auto_restart import poll_once
    calls = []
    state['baseline_sha'] = 'aaa111'
    v = poll_once(state, **_seams(
        head_sha=lambda: 'bbb222',
        running_tasks=lambda: [{'taskId': 't1'}, {'taskId': 't2'}],
        do_restart=lambda r: calls.append(r) or True))
    assert v == 'not-ready:tasks-running:2'
    assert calls == []
    assert state['restarting'] is False


@pytest.mark.unit
def test_head_move_with_shutdown_requested_defers(state):
    from lib.auto_restart import poll_once

    class _Flag:
        def is_set(self):
            return True

    state['baseline_sha'] = 'aaa111'
    v = poll_once(state, **_seams(head_sha=lambda: 'bbb222',
                                  shutdown_requested=_Flag()))
    assert v == 'not-ready:shutdown-requested'


@pytest.mark.unit
def test_precondition_error_fails_closed(state):
    from lib.auto_restart import poll_once

    def boom():
        raise RuntimeError('registry down')

    state['baseline_sha'] = 'aaa111'
    v = poll_once(state, **_seams(head_sha=lambda: 'bbb222',
                                  running_tasks=boom))
    assert v == 'not-ready:check-error:RuntimeError'


@pytest.mark.unit
def test_all_green_triggers_restart(state):
    from lib.auto_restart import poll_once
    calls = []
    state['baseline_sha'] = 'aaa111'
    v = poll_once(state, **_seams(
        head_sha=lambda: 'bbb222',
        do_restart=lambda r: calls.append(r) or True))
    assert v == 'restart-triggered'
    assert calls == ['auto_restart_head_changed']
    assert state['restarting'] is True
    # Terminal: no further restarts from this watcher instance.
    assert poll_once(state, **_seams(head_sha=lambda: 'ccc333')) == 'restarting'


@pytest.mark.unit
def test_busy_then_drained_retries_and_fires(state):
    """A HEAD move arriving while busy is DEFERRED, not dropped: the next
    poll after the server drains fires the restart."""
    from lib.auto_restart import poll_once
    calls = []
    state['baseline_sha'] = 'aaa111'
    busy = {'running': [{'taskId': 't1'}]}
    v1 = poll_once(state, **_seams(
        head_sha=lambda: 'bbb222',
        running_tasks=lambda: busy['running'],
        do_restart=lambda r: calls.append(r) or True))
    assert v1.startswith('not-ready:')
    busy['running'] = []
    v2 = poll_once(state, **_seams(
        head_sha=lambda: 'bbb222',
        running_tasks=lambda: busy['running'],
        do_restart=lambda r: calls.append(r) or True))
    assert v2 == 'restart-triggered'
    assert calls == ['auto_restart_head_changed']


@pytest.mark.unit
def test_restart_failure_keeps_watching(state):
    from lib.auto_restart import poll_once
    state['baseline_sha'] = 'aaa111'
    v = poll_once(state, **_seams(head_sha=lambda: 'bbb222',
                                  do_restart=lambda r: False))
    assert v == 'error'
    assert state['restarting'] is False


@pytest.mark.unit
def test_not_a_repo(state):
    from lib.auto_restart import poll_once
    assert poll_once(state, **_seams(is_repo=lambda: False)) == 'no-repo'


@pytest.mark.unit
def test_unreadable_head_is_transient(state):
    from lib.auto_restart import poll_once
    assert poll_once(state, **_seams(head_sha=lambda: None)) == 'no-head'


@pytest.mark.unit
def test_poll_error_is_contained(state):
    from lib.auto_restart import poll_once

    def boom():
        raise OSError('git gone')

    assert poll_once(state, **_seams(head_sha=boom)) == 'error'
    assert state['restarting'] is False


@pytest.mark.unit
def test_interval_floor_and_default(monkeypatch):
    import lib.auto_restart as ar
    monkeypatch.delenv('TOFU_AUTO_RESTART_INTERVAL_SEC', raising=False)
    assert ar._interval_sec() == 60.0
    monkeypatch.setenv('TOFU_AUTO_RESTART_INTERVAL_SEC', '5')
    assert ar._interval_sec() == 10.0  # floor
    monkeypatch.setenv('TOFU_AUTO_RESTART_INTERVAL_SEC', 'not-a-number')
    assert ar._interval_sec() == 60.0


@pytest.mark.unit
def test_deferred_reexec_delegates_to_shared_primitive(monkeypatch):
    """Wire-parity pin for the update.py extraction: _deferred_reexec must
    delegate to _perform_server_reexec('update')."""
    from routes.api_v1 import update as upd
    calls = []
    monkeypatch.setattr(upd, '_perform_server_reexec',
                        lambda reason: calls.append(reason) or True)
    upd._deferred_reexec(delay=0)
    assert calls == ['update']

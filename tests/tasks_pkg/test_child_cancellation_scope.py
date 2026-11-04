"""A graph shares its parent observer; every child retains its own Stop/deadline."""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.agent_core.abort_signal import AbortSignal

pytestmark = pytest.mark.unit


def _task(index):
    return {'id': f'child-{index}', 'config': {}, 'abort_event': AbortSignal()}


def test_one_parent_observation_cancels_all_children_and_releases_observer():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    entered, release = threading.Event(), threading.Event()
    calls = []

    def source():
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(3)
        return True

    scope = ChildCancellationScope(source)
    children = [_task(i) for i in range(64)]
    watches = [scope.watch(child) for child in children]
    try:
        assert entered.wait(2)
        assert scope.observer_count == 1
        assert len(calls) == 1
        release.set()
        assert all(child['abort_event'].wait(2) for child in children)
        assert all(child['aborted'] for child in children)
    finally:
        release.set()
        for watch in watches:
            watch.close()
    assert scope.observer_count == 0
    assert scope.child_count == 0


def test_released_child_cannot_be_signaled_by_late_parent_probe():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    entered, release = threading.Event(), threading.Event()

    def source():
        entered.set()
        assert release.wait(3)
        return True

    scope = ChildCancellationScope(source)
    removed, kept = _task(1), _task(2)
    first, second = scope.watch(removed), scope.watch(kept)
    try:
        assert entered.wait(2)
        first.close()
        release.set()
        assert kept['abort_event'].wait(2)
        assert not removed['abort_event'].is_set()
    finally:
        release.set()
        first.close()
        second.close()
    assert scope.child_count == scope.observer_count == 0


def test_child_deadline_and_local_stop_never_cancel_siblings():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    scope = ChildCancellationScope(lambda: False)
    expired, stopped, sibling = [_task(i) for i in range(3)]
    expired['_invocationDeadline'] = 0
    stopped['abort_event'].set()
    watches = [scope.watch(task) for task in (expired, stopped, sibling)]
    try:
        assert expired['abort_event'].wait(2)
        assert expired['_invocationStopReason'] == 'deadline'
        assert not sibling['abort_event'].is_set()
    finally:
        for watch in watches:
            watch.close()


def test_last_release_joins_its_probe_without_blocking_a_later_wave():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    entered, release, later_observed, closed = [threading.Event() for _ in range(4)]

    def source():
        if not entered.is_set():
            entered.set()
            assert release.wait(3)
            return True
        later_observed.set()
        return False

    scope = ChildCancellationScope(source)
    old, later = _task('old'), _task('later')
    first = scope.watch(old)
    generation = first._generation
    second = None
    def close_first():
        first.close()
        closed.set()
    closer = threading.Thread(target=close_first)
    try:
        assert entered.wait(2)
        closer.start()
        assert generation.stop.wait(2)
        assert not closed.is_set(), 'the last borrow must join the in-flight observation'
        second = scope.watch(later)
        assert later_observed.wait(2), 'retirement must not hold the scope lock'
        release.set()
        assert closed.wait(2)
        assert not old['abort_event'].is_set()
        assert not later['abort_event'].is_set()
    finally:
        release.set()
        if closer.ident is not None:
            closer.join()
        else:
            first.close()
        if second is not None:
            second.close()
    assert scope.child_count == scope.observer_count == 0


def test_lost_control_port_fails_closed_with_original_failure():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    failure = OSError('owner control lost')

    def source():
        raise failure

    scope = ChildCancellationScope(source)
    task = _task(0)
    with scope.watch(task):
        assert task['abort_event'].wait(2)
        assert task['_invocationDeliveryError'] is failure
        assert task['aborted']


def test_scope_can_be_reused_across_waves_and_concurrent_retirement():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    scope = ChildCancellationScope(lambda: False)

    def borrow(index):
        task = _task(index)
        with scope.watch(task):
            assert not scope()
        assert not task['abort_event'].is_set()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(borrow, range(64)))
    assert scope.child_count == scope.observer_count == 0
    with scope.watch(_task('later')):
        assert scope.child_count == scope.observer_count == 1
    assert scope.child_count == scope.observer_count == 0


def test_observer_start_failure_releases_membership(monkeypatch):
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    scope = ChildCancellationScope(lambda: False)

    def fail_start(_):
        raise RuntimeError('start refused')

    monkeypatch.setattr(threading.Thread, 'start', fail_start)
    with pytest.raises(RuntimeError, match='start refused'):
        scope.watch(_task(0))
    assert scope.child_count == scope.observer_count == 0


def test_owner_signal_wakes_observer_and_subscription_is_released():
    from lib.tasks_pkg.invocation_cancellation import ChildCancellationScope
    signal = AbortSignal()
    scope = ChildCancellationScope(signal.is_set, source_signal=signal)
    child = _task(0)
    with scope.watch(child):
        signal.set()
        assert child['abort_event'].wait(2)
    assert signal._observers is None
    assert scope.child_count == scope.observer_count == 0

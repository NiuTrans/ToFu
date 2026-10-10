"""Optional terminal adapters have demand registration and joined teardown."""
import threading

import pytest

from lib.tasks_pkg.manager import _terminal as terminal

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def observers(monkeypatch):
    monkeypatch.setattr(terminal, '_TERMINAL_LISTENERS', [])
    monkeypatch.setattr(terminal, '_TERMINAL_REGISTRATIONS', {})
    monkeypatch.setattr(terminal, '_TERMINAL_WITHDRAWN', set())
    monkeypatch.setattr('lib.agent_core.terminal_delivery.deliver_terminal', lambda task: None)
    yield
    for listener in tuple(terminal._TERMINAL_LISTENERS):
        terminal.unregister_terminal_listener(listener)
    assert not terminal._TERMINAL_REGISTRATIONS
    assert not terminal._TERMINAL_WITHDRAWN


def task(identifier='task'):
    return {'id': identifier, 'status': 'done', 'finished_at': 1}


def test_demand_registration_dedup_and_failure_isolation():
    received = []
    listeners = [lambda value, index=index: received.append(index) for index in range(65)]
    for listener in listeners:
        assert terminal.register_terminal_listener(listener)
        assert not terminal.register_terminal_listener(listener)
    value = task()
    terminal.notify_terminal_listeners(value)
    terminal.notify_terminal_listeners(value)
    assert received == list(range(65))
    def broken(value):
        raise RuntimeError('synthetic')
    terminal.register_terminal_listener(broken)
    terminal.register_terminal_listener(lambda value: received.append('after-fault'))
    terminal.notify_terminal_listeners(task('next'))
    assert received[-1] == 'after-fault'


def test_external_unregister_joins_active_callback():
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    def listener(value):
        entered.set()
        assert release.wait(5)
    terminal.register_terminal_listener(listener)
    delivery = threading.Thread(target=terminal.notify_terminal_listeners, args=(task(),))
    delivery.start()
    assert entered.wait(5)
    def withdraw():
        assert terminal.unregister_terminal_listener(listener)
        returned.set()
    teardown = threading.Thread(target=withdraw)
    teardown.start()
    try:
        assert not returned.wait(.1)
    finally:
        release.set()
        delivery.join(5)
        teardown.join(5)
    assert returned.is_set()
    assert not delivery.is_alive() and not teardown.is_alive()
    assert not terminal.unregister_terminal_listener(listener)


def test_withdraw_and_reregister_do_not_receive_old_snapshot():
    received = []
    def later(value):
        received.append(value['id'])
    def first(value):
        assert terminal.unregister_terminal_listener(later)
        assert terminal.register_terminal_listener(later)
    terminal.register_terminal_listener(first)
    terminal.register_terminal_listener(later)
    terminal.notify_terminal_listeners(task('old'))
    assert not received
    terminal.unregister_terminal_listener(first)
    terminal.notify_terminal_listeners(task('new'))
    assert received == ['new']


def test_self_unregister_and_bound_method_identity():
    received = []
    class Observer:
        def receive(self, value):
            received.append(value['id'])
            assert terminal.unregister_terminal_listener(self.receive)
    observer = Observer()
    assert terminal.register_terminal_listener(observer.receive)
    assert not terminal.register_terminal_listener(observer.receive)
    terminal.notify_terminal_listeners(task())
    terminal.notify_terminal_listeners(task('next'))
    assert received == ['task']
    assert not terminal._TERMINAL_REGISTRATIONS
    assert not terminal._TERMINAL_WITHDRAWN


def test_callbacks_can_withdraw_each_other_without_deadlock():
    barrier = threading.Barrier(2)
    results = []
    def first(value):
        if value['id'] == 'first':
            barrier.wait(5)
            results.append(terminal.unregister_terminal_listener(second))
    def second(value):
        if value['id'] == 'second':
            barrier.wait(5)
            results.append(terminal.unregister_terminal_listener(first))
    terminal.register_terminal_listener(first)
    terminal.register_terminal_listener(second)
    threads = [threading.Thread(target=terminal.notify_terminal_listeners, args=(task(name),))
               for name in ('first', 'second')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert results == [True, True]
    assert not terminal._TERMINAL_REGISTRATIONS
    assert not terminal._TERMINAL_WITHDRAWN


def test_external_teardown_joins_an_already_self_withdrawn_callback():
    withdrawn, release, returned = threading.Event(), threading.Event(), threading.Event()
    def listener(value):
        assert terminal.unregister_terminal_listener(listener)
        withdrawn.set()
        assert release.wait(5)
    terminal.register_terminal_listener(listener)
    delivery = threading.Thread(target=terminal.notify_terminal_listeners, args=(task(),))
    delivery.start()
    assert withdrawn.wait(5)
    def teardown():
        assert not terminal.unregister_terminal_listener(listener)
        returned.set()
    closer = threading.Thread(target=teardown)
    closer.start()
    try:
        assert not returned.wait(.1)
    finally:
        release.set()
        delivery.join(5)
        closer.join(5)
    assert returned.is_set()
    assert not delivery.is_alive() and not closer.is_alive()

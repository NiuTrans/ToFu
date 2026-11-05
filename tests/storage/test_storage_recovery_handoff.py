"""Recovery publishes only a live generation and never loses a crash wakeup."""
from __future__ import annotations

import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime
from lib.storage.supervisor import StorageSupervisor

pytestmark = pytest.mark.unit


class RestartSupervisor:
    def __init__(self):
        self.ready = False
        self.starts = 0
        self.callback = None
        self.client = self
        self.recovered = threading.Event()

    def add_crash_callback(self, callback):
        self.callback = callback

    def start(self):
        self.starts += 1
        self.ready = True
        if self.starts >= 2:
            self.recovered.set()
        return self

    def health(self):
        return {'ready': True}

    def crash(self):
        self.ready = False
        self.callback(-9)

    def stop(self, timeout=10):
        self.ready = False


@pytest.mark.parametrize('crash_at', ['handshake', 'worker_handoff'])
def test_crash_during_restart_is_not_lost(monkeypatch, crash_at):
    # Exactly two observed exits are allowed. A crash during start must not
    # also be counted as an independent failed spawn and trip the breaker.
    monkeypatch.setenv('TOFU_STORAGE_RESTART_CRASH_LIMIT', '2')
    supervisor = RestartSupervisor()
    runtime = StorageRuntime(supervisor)
    original_start = runtime._start

    def health():
        if supervisor.starts == 1:
            supervisor.crash()
        # A response received before death can still say ready.
        return {'ready': True}

    def start(*, clear_stop):
        client = original_start(clear_stop=clear_stop)
        if supervisor.starts == 1:
            supervisor.crash()
        return client

    if crash_at == 'handshake':
        monkeypatch.setattr(supervisor, 'health', health)
    else:
        monkeypatch.setattr(runtime, '_start', start)
    try:
        supervisor.crash()
        assert supervisor.recovered.wait(3), 'the replacement crash lost its restart owner'
        assert runtime.client() is supervisor
        assert runtime.ready
    finally:
        runtime.stop(timeout=3)


def test_supervisor_readiness_race_returns_typed_storage_failure(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    supervisor = StorageSupervisor()
    with pytest.raises(StorageError) as error:
        _ = supervisor.client
    assert error.value.code == 'database_unavailable'
    assert error.value.request_not_dispatched


def test_dead_child_closes_old_client_before_recovery_callback(monkeypatch):
    monkeypatch.delenv('TOFU_STORAGE_CONNECTION_FILE', raising=False)
    supervisor = StorageSupervisor()
    events = []

    class Child:
        stdin = None
        pid = 123

        def poll(self):
            return -9

    class Client:
        def close(self):
            events.append('closed')

    child = Child()
    supervisor._process = child
    supervisor._client = Client()
    supervisor.add_crash_callback(lambda code: events.append(('restart', code)))
    supervisor._monitor_process(child)
    assert events == ['closed', ('restart', -9)]


def test_retained_native_client_uses_recovered_authority_without_new_receipt(monkeypatch):
    from lib.storage.v2_client import StorageV2Client

    old = StorageV2Client('127.0.0.1', 12345, b'a' * 48, owner_id=7, tenant_id=3)
    new = StorageV2Client('127.0.0.1', 12346, b'b' * 48, owner_id=7, tenant_id=3)
    monkeypatch.setattr(old, 'health', lambda: {'ready': True})
    monkeypatch.setattr(new, 'health', lambda: {'ready': True})
    supervisor = RestartSupervisor()

    def start():
        supervisor.starts += 1
        supervisor.ready = True
        supervisor.client = old if supervisor.starts == 1 else new
        return supervisor.client

    supervisor.start = start
    runtime = StorageRuntime(supervisor)
    seen = []

    def lost_ack(operation, payload, **kwargs):
        seen.append(('old', kwargs['command_id']))
        supervisor.crash()
        raise StorageError('database_unavailable', 'lost committed response', True, 1)

    def replay_receipt(operation, payload, **kwargs):
        seen.append(('new', kwargs['command_id']))
        return {'version': 1}

    monkeypatch.setattr(old, '_attempt', lost_ack)
    monkeypatch.setattr(new, '_attempt', replay_receipt)
    try:
        retained = runtime.start()
        assert retained.command('record.put', {'namespace': 'n', 'key': 'k'}, 'stable-receipt') == {'version': 1}
        assert seen == [('old', 'stable-receipt'), ('new', 'stable-receipt')]
    finally:
        runtime.stop(timeout=3)
        old.close()
        new.close()

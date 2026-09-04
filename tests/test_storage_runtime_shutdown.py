"""Storage shutdown must not certify while an auto-restart can reacquire."""

from __future__ import annotations

import threading

import pytest

from lib.storage.errors import StorageError
from lib.storage.runtime import StorageRuntime


pytestmark = pytest.mark.unit


class _BlockingRestartSupervisor:
    """Minimal supervisor double for the restart-vs-stop race."""

    ready = False

    def __init__(self) -> None:
        self.callback = None
        self.start_entered = threading.Event()
        self.release_start = threading.Event()

    def add_crash_callback(self, callback) -> None:
        self.callback = callback

    def start(self):
        self.start_entered.set()
        self.release_start.wait(5)
        return self

    def health(self):
        return {'ready': True}

    def stop(self, timeout=10.0) -> None:
        self.ready = False

    def status(self):
        return {
            'ready': self.ready,
            'state': 'ready' if self.ready else 'stopped',
            'backend': 'sqlite',
            'pid': None,
            'port': None,
            'last_exit_code': None,
        }


def test_stop_fails_closed_while_restart_worker_is_still_starting():
    supervisor = _BlockingRestartSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(9)
    assert supervisor.start_entered.wait(1)

    with pytest.raises(StorageError, match='restart worker did not stop'):
        runtime.stop(timeout=0.1)

    supervisor.release_start.set()
    thread = runtime._restart_thread
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert runtime.ready is False


def test_late_restart_handshake_cannot_publish_ready_after_stop():
    supervisor = _BlockingRestartSupervisor()
    runtime = StorageRuntime(supervisor)
    runtime._crashed(9)
    assert supervisor.start_entered.wait(1)

    errors = []

    def stop_runtime():
        try:
            runtime.stop(timeout=2)
        except Exception as exc:  # pragma: no cover - assertion reports detail
            errors.append(exc)

    stopper = threading.Thread(target=stop_runtime)
    stopper.start()
    supervisor.release_start.set()
    stopper.join(timeout=3)

    assert not stopper.is_alive()
    assert errors == []
    assert runtime.ready is False
    assert runtime.status()['state'] == 'stopped'

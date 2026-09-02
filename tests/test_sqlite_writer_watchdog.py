"""SQLite writer stalls expose their phase and recover in bounded time."""

from __future__ import annotations

import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters.sqlite import _FairWriter


pytestmark = pytest.mark.unit


class _CommitStallConnection:
    def __init__(self):
        self.commit_started = threading.Event()
        self.release_commit = threading.Event()
        self.interrupts = 0

    def set_progress_handler(self, _callback, _instructions):
        return None

    def execute(self, _sql, _params=()):
        return None

    def commit(self):
        self.commit_started.set()
        self.release_commit.wait(2)

    def rollback(self):
        return None

    def interrupt(self):
        self.interrupts += 1

    def close(self):
        return None


def test_commit_stall_is_phase_labeled_and_triggers_bounded_restart():
    connection = _CommitStallConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=0.05,
        stall_grace_s=0.05,
        hard_kill_s=0.15,
        watchdog_interval_s=0.01,
    )
    hard_exit = threading.Event()
    writer._hard_exit = lambda _reason: hard_exit.set()
    try:
        with pytest.raises(StorageError, match='deadline expired'):
            writer.submit(
                lambda _session: {'ok': True},
                'user',
                time.monotonic() + 0.05,
                operation_name='watchdog.commit-stall',
            )

        assert connection.commit_started.wait(1)
        assert hard_exit.wait(1), 'watchdog did not enforce the hard bound'
        assert connection.interrupts >= 1
        stall = writer.last_stall()
        assert stall is not None
        assert stall['operation'] == 'watchdog.commit-stall'
        assert stall['phase'] == 'commit'
        assert writer.metrics['stall_interrupts'] == 1
    finally:
        connection.release_commit.set()
        writer.close()

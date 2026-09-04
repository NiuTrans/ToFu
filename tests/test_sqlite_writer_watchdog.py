"""SQLite writer stalls expose their phase and recover in bounded time."""

from __future__ import annotations

import gc
import sqlite3
import threading
import time
import weakref

import pytest

from lib.storage.errors import StorageError
from lib.storage_sidecar.adapters import sqlite as sqlite_adapter
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


def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail('condition did not become true before timeout')


def _submit_in_thread(writer, operation, errors):
    try:
        writer.submit(
            operation,
            'user',
            time.monotonic() + 5,
            operation_name='writer-budget.background',
        )
    except BaseException as exc:
        errors.append(exc)


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


def test_one_operation_may_extend_its_transaction_watchdog_without_global_change():
    connection = sqlite3.connect(
        ':memory:', isolation_level=None, check_same_thread=False)
    writer = _FairWriter(
        connection,
        transaction_timeout_s=0.05,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    try:
        result = writer.submit(
            lambda _session: time.sleep(0.08) or 'completed',
            'maintenance',
            time.monotonic() + 1,
            operation_name='maintenance.extended-budget',
            transaction_timeout_s=0.5,
        )
        assert result == 'completed'
        with pytest.raises(StorageError) as default_timeout:
            writer.submit(
                lambda _session: time.sleep(0.08),
                'maintenance',
                time.monotonic() + 1,
                operation_name='maintenance.default-budget',
            )
        assert default_timeout.value.code == 'database_timeout'
        with pytest.raises(StorageError) as raised:
            writer.submit(
                lambda _session: pytest.fail('invalid budget reached operation'),
                'maintenance',
                time.monotonic() + 1,
                transaction_timeout_s=301.0,
            )
        assert raised.value.code == 'database_protocol_error'
    finally:
        writer.close()


def test_writer_queue_rejects_before_retaining_more_work():
    connection = _CommitStallConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        queue_capacity=1,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    errors = []
    first = threading.Thread(
        target=_submit_in_thread,
        args=(writer, lambda _session: {'first': True}, errors),
    )
    second = threading.Thread(
        target=_submit_in_thread,
        args=(writer, lambda _session: {'second': True}, errors),
    )
    try:
        first.start()
        assert connection.commit_started.wait(1)
        second.start()
        _wait_for(lambda: writer.queue_depths()['user'] == 1)

        started = time.monotonic()
        with pytest.raises(StorageError) as raised:
            writer.submit(
                lambda _session: pytest.fail('rejected work executed'),
                'user',
                time.monotonic() + 5,
                operation_name='writer-budget.rejected',
            )
        assert raised.value.code == 'database_busy'
        assert raised.value.retryable is True
        assert time.monotonic() - started < 0.2
        assert writer.metrics['queue_rejections'] == 1
        assert writer.metrics['submitted'] == 2
        assert writer.metrics['max_queue_depth'] == 1
    finally:
        connection.release_commit.set()
        first.join(2)
        second.join(2)
        writer.close()
    assert not errors


def test_writer_rechecks_resource_admission_after_queue_acquisition():
    connection = _CommitStallConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    admission_checks = 0
    operation_executed = threading.Event()

    def admit_once() -> None:
        nonlocal admission_checks
        admission_checks += 1
        if admission_checks > 1:
            raise StorageError(
                'database_busy',
                'Fastpath WAL write-pressure threshold reached',
                True,
                250,
            )

    writer.set_write_admission_hook(admit_once)
    try:
        with pytest.raises(StorageError) as raised:
            writer.submit(
                lambda _session: operation_executed.set(),
                'user',
                time.monotonic() + 5,
                operation_name='writer-budget.rebase-pressure',
            )
        assert raised.value.code == 'database_busy'
        assert raised.value.retryable is True
        assert raised.value.retry_after_ms == 250
        assert admission_checks == 2
        assert not operation_executed.is_set()
        assert not connection.commit_started.is_set()
        assert writer.metrics['write_admission_rejections'] == 1

        # The fence must never reject the raw TRUNCATE checkpoint that creates
        # a fresh WAL and releases the pressure condition.
        assert writer.submit(
            lambda _session: 'checkpointed',
            'maintenance',
            time.monotonic() + 5,
            operation_name='fastpath.checkpoint',
            raw=True,
        ) == 'checkpointed'
        assert admission_checks == 2
    finally:
        writer.close()


def test_acquisition_timeout_removes_payload_closure_immediately(monkeypatch):
    connection = _CommitStallConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        queue_capacity=1,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    errors = []
    first = threading.Thread(
        target=_submit_in_thread,
        args=(writer, lambda _session: {'first': True}, errors),
    )
    monkeypatch.setitem(sqlite_adapter._ACQUIRE_CAP_S, 'user', 0.05)

    class Payload:
        pass

    payload = Payload()
    payload_reference = weakref.ref(payload)
    executed = threading.Event()

    def queued_operation(_session, retained_payload=payload):
        executed.set()
        return retained_payload

    try:
        first.start()
        assert connection.commit_started.wait(1)

        with pytest.raises(StorageError) as raised:
            writer.submit(
                queued_operation,
                'user',
                time.monotonic() + 5,
                operation_name='writer-budget.timeout',
            )
        assert raised.value.code == 'database_timeout'
        assert writer.queue_depths()['user'] == 0
        assert writer.metrics['timed_out'] == 1
        assert writer.metrics['cancelled_before_start'] == 1

        del raised
        del queued_operation
        del payload
        gc.collect()
        assert payload_reference() is None
        assert not executed.is_set()
    finally:
        connection.release_commit.set()
        first.join(2)
        writer.close()
    assert not errors
    assert not executed.is_set()

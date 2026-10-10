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
        self.closed = False

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
        self.closed = True


def _wait_for(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    pytest.fail('condition did not become true before timeout')


def _sqlite_fault(code: int, message: str = 'injected fault') -> sqlite3.Error:
    error = sqlite3.OperationalError(message)
    error.sqlite_errorcode = code
    return error


class _RollbackFailConnection:
    """Physical writer whose ROLLBACK always fails (fault injection)."""

    def __init__(self):
        self.begins = 0
        self.rollbacks = 0
        self.closed = False

    def set_progress_handler(self, _callback, _instructions):
        return None

    def execute(self, sql, _params=()):
        if str(sql).upper().startswith('BEGIN'):
            self.begins += 1

    def commit(self):
        return None

    def rollback(self):
        self.rollbacks += 1
        raise sqlite3.OperationalError('rollback failed')

    def interrupt(self):
        return None

    def close(self):
        self.closed = True


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


def test_writer_backlog_accepts_above_legacy_queue_hint():
    connection = _CommitStallConnection()
    writer = _FairWriter(connection, transaction_timeout_s=5, queue_capacity=1,
                         stall_grace_s=10, hard_kill_s=20)
    errors, observed = [], []
    first = threading.Thread(target=_submit_in_thread,
        args=(writer, lambda _session: observed.append(0), errors))
    followers = []
    try:
        first.start()
        assert connection.commit_started.wait(1)
        for index in range(1, 33):
            follower = threading.Thread(target=_submit_in_thread,
                args=(writer, lambda _session, index=index: observed.append(index), errors))
            follower.start()
            followers.append(follower)
            _wait_for(lambda: writer.queue_depths()['user'] == index)
        assert writer.metrics['queue_rejections'] == 0
        assert writer.metrics['submitted'] == 33
        assert writer.metrics['max_queue_depth'] == 32
        connection.release_commit.set()
    finally:
        connection.release_commit.set()
        first.join(5)
        for follower in followers:
            follower.join(5)
            assert not follower.is_alive()
        writer.close()
    assert not errors and observed == list(range(33))


def test_physical_full_error_before_begin_preserves_failure_without_callback():
    class FullConnection(_CommitStallConnection):
        def execute(self, sql, params=()):
            if sql == 'BEGIN IMMEDIATE':
                error = sqlite3.OperationalError('database or disk is full')
                error.sqlite_errorcode = sqlite3.SQLITE_FULL
                raise error
            return super().execute(sql, params)
    connection = FullConnection()
    writer = _FairWriter(connection, transaction_timeout_s=5,
                         stall_grace_s=10, hard_kill_s=20)
    operation_executed = threading.Event()
    try:
        with pytest.raises(StorageError) as raised:
            writer.submit(lambda _session: operation_executed.set(),
                          'user', time.monotonic() + 5,
                          operation_name='writer-physical-full')
        assert raised.value.code == 'database_unavailable'
        assert raised.value.retryable is False
        assert 'full' in raised.value.message.lower()
        assert not operation_executed.is_set()
        assert not connection.commit_started.is_set()
        assert writer.metrics['transaction_retries'] == 0
        assert writer.submit(lambda _session: 'checkpointed', 'maintenance',
                             time.monotonic() + 5, raw=True) == 'checkpointed'
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
                time.monotonic() + .05,
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


def test_writer_rollback_failure_fences_the_poisoned_writer(caplog):
    """A failed ROLLBACK leaves the writer's transaction state unknown.

    The writer must log an ERROR, escalate through the hard-exit seam, and
    refuse every later job loudly — never silently serve another BEGIN
    IMMEDIATE that would fail as a misleading non-retryable internal error.
    """
    connection = _RollbackFailConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    hard_exit = threading.Event()
    writer._hard_exit = lambda _reason: hard_exit.set()
    try:
        with caplog.at_level('ERROR', logger='tofu.storage.sidecar.sqlite'):
            with pytest.raises(StorageError) as raised:
                writer.submit(
                    lambda _session: (_ for _ in ()).throw(
                        ValueError('semantic failure')),
                    'user',
                    time.monotonic() + 5,
                    operation_name='fault.rollback',
                )
        # The current job keeps its own classified (visible) failure.
        assert raised.value.code == 'database_internal'
        assert hard_exit.wait(1), 'rollback failure did not reach the hard-exit seam'
        assert writer._writer_fatal_reason == 'writer-rollback-failed'
        assert writer.metrics['rollback_failures'] == 1
        assert connection.rollbacks == 1
        assert 'poisoned' in caplog.text

        # The poisoned writer never serves another job: the submission fails
        # fast and no new BEGIN IMMEDIATE is attempted on the connection.
        with pytest.raises(StorageError) as poisoned:
            writer.submit(
                lambda _session: pytest.fail('poisoned writer executed a job'),
                'user',
                time.monotonic() + 5,
                operation_name='fault.after-poison',
            )
        assert poisoned.value.code == 'database_unavailable'
        assert 'poisoned' in poisoned.value.message
        assert connection.begins == 1
    finally:
        writer.close()


class _BatchPoisonConnection(_RollbackFailConnection):
    """The first commit (blocker) lands; later commits fail retryable-busy,
    and the batch outer ROLLBACK then fails (fault injection)."""

    def __init__(self):
        super().__init__()
        self.commits = 0

    def commit(self):
        self.commits += 1
        if self.commits > 1:
            raise _sqlite_fault(sqlite3.SQLITE_BUSY, 'commit busy')


def test_batch_rollback_failure_fences_the_writer_without_retry_storm(caplog):
    """Batch path: a failed outer ROLLBACK gets the same fatal treatment.

    Per-job SAVEPOINT isolation is preserved (both operations ran inside one
    segment), but after the poisoned rollback the batch loop must not spin
    its bounded retries re-entering BEGIN on unknown state: survivors resolve
    with the classified poisoned-writer error after ONE batch BEGIN.
    """
    connection = _BatchPoisonConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    hard_exit = threading.Event()
    writer._hard_exit = lambda _reason: hard_exit.set()
    blocker_started = threading.Event()
    release_blocker = threading.Event()
    errors = []

    def blocker(_session):
        blocker_started.set()
        release_blocker.wait(2)
        return 'blocked'

    def submit_in_thread(operation, name):
        try:
            writer.submit(
                operation, 'user', time.monotonic() + 5, operation_name=name)
        except BaseException as exc:
            errors.append(exc)

    blocker_thread = threading.Thread(
        target=submit_in_thread, args=(blocker, 'fault.blocker'))
    first = threading.Thread(
        target=submit_in_thread, args=(lambda _s: 'one', 'fault.batch-1'))
    second = threading.Thread(
        target=submit_in_thread, args=(lambda _s: 'two', 'fault.batch-2'))
    try:
        blocker_thread.start()
        assert blocker_started.wait(1)
        first.start()
        second.start()
        _wait_for(lambda: writer.queue_depths()['user'] == 2)
        with caplog.at_level('ERROR', logger='tofu.storage.sidecar.sqlite'):
            release_blocker.set()
            first.join(2)
            second.join(2)
        blocker_thread.join(2)

        assert hard_exit.wait(1), 'batch rollback failure did not hard-exit'
        assert writer._writer_fatal_reason == 'batch-rollback-failed'
        assert writer.metrics['rollback_failures'] == 1
        assert connection.rollbacks == 1
        assert 'poisoned' in caplog.text
        # Both batched jobs resolved with the classified poisoned-writer
        # error; the busy-retry loop did not re-enter BEGIN on the poisoned
        # connection (one BEGIN for the blocker, one for the batch).
        assert len(errors) == 2
        for error in errors:
            assert isinstance(error, StorageError)
            assert error.code == 'database_unavailable'
            assert 'poisoned' in error.message
        assert connection.begins == 2
        assert connection.commits == 2
    finally:
        release_blocker.set()
        blocker_thread.join(2)
        first.join(2)
        second.join(2)
        writer.close()


def test_close_leaves_the_connection_open_when_the_writer_is_wedged(caplog):
    """A FUSE-wedged writer can outlive the close join budget.

    Closing its check_same_thread=False connection would let it wake up
    executing on a closed handle; close() must instead warn and leave the
    handle to process teardown.
    """
    connection = _CommitStallConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        stall_grace_s=10,
        hard_kill_s=20,
        close_join_timeout_s=0.05,
    )
    errors = []
    first = threading.Thread(
        target=_submit_in_thread,
        args=(writer, lambda _session: {'ok': True}, errors),
    )
    try:
        first.start()
        assert connection.commit_started.wait(1)
        with caplog.at_level('WARNING', logger='tofu.storage.sidecar.sqlite'):
            started = time.monotonic()
            writer.close()
        assert time.monotonic() - started < 1
        assert 'did not stop' in caplog.text
        assert connection.closed is False
    finally:
        connection.release_commit.set()
        first.join(2)
    assert not errors


def test_close_still_closes_the_connection_after_a_clean_stop():
    connection = _CommitStallConnection()
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        stall_grace_s=10,
        hard_kill_s=20,
        close_join_timeout_s=1,
    )
    writer.close()
    assert connection.closed is True


def test_sqlite_full_fails_fast_without_burning_backoff_retries():
    """SQLITE_FULL is a resource condition, not a transient busy state.

    Classic mode has no write-admission fence, so a full volume must surface
    as an accurate non-retryable error after ONE attempt instead of
    retry-storming the sole writer through its bounded backoff budget.
    """
    mapped = sqlite_adapter._map_sqlite_error(
        _sqlite_fault(sqlite3.SQLITE_FULL), time.monotonic() + 5)
    assert mapped.code == 'database_unavailable'
    assert mapped.retryable is False
    assert 'full' in mapped.message

    connection = sqlite3.connect(
        ':memory:', isolation_level=None, check_same_thread=False)
    writer = _FairWriter(
        connection,
        transaction_timeout_s=5,
        stall_grace_s=10,
        hard_kill_s=20,
    )
    try:
        with pytest.raises(StorageError) as raised:
            writer.submit(
                lambda _session: (_ for _ in ()).throw(
                    _sqlite_fault(sqlite3.SQLITE_FULL)),
                'user',
                time.monotonic() + 5,
                operation_name='fault.disk-full',
            )
        assert raised.value.code == 'database_unavailable'
        assert raised.value.retryable is False
        assert writer.metrics['transaction_retries'] == 0
    finally:
        writer.close()


def test_user_writer_wait_uses_caller_deadline_beyond_old_two_second_cap():
    class Connection(_CommitStallConnection):
        def commit(self):
            self.commit_started.set()
            assert self.release_commit.wait(8)
    connection = Connection()
    writer = _FairWriter(connection, transaction_timeout_s=5, queue_capacity=1,
                         stall_grace_s=10, hard_kill_s=20)
    errors = []
    first = threading.Thread(target=_submit_in_thread,
        args=(writer, lambda session: 1, errors))
    second = threading.Thread(target=_submit_in_thread,
        args=(writer, lambda session: 2, errors))
    try:
        first.start()
        assert connection.commit_started.wait(1)
        second.start()
        _wait_for(lambda: writer.queue_depths()['user'] == 1)
        # Actual blocked transaction exceeds the retired user-lane cap.
        time.sleep(2.2)
        assert second.is_alive() and not errors
        connection.release_commit.set()
        first.join(3)
        second.join(3)
        assert not first.is_alive() and not second.is_alive() and not errors
    finally:
        connection.release_commit.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        writer.close()

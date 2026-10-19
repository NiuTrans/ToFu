"""A real SQLite commit with a lost acknowledgement never repeats unreceipted work."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.adapters.sqlite import SQLiteBackend
from tests.storage_authority_sql.test_sqlite_read_pool import _config


pytestmark = pytest.mark.unit


class CommitFault:
    """Keep real transaction state; fail once before or after the actual COMMIT."""

    def __init__(self, connection, *, phase, commit_number=1):
        self.connection = connection
        self.phase = phase
        self.commit_number = commit_number
        self.commits = 0

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def commit(self):
        self.commits += 1
        if self.commits != self.commit_number:
            return self.connection.commit()
        if self.phase == 'after':
            self.connection.commit()
        error = sqlite3.OperationalError('injected commit acknowledgement loss')
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error


@pytest.fixture
def backend(tmp_path):
    value = SQLiteBackend(_config(tmp_path))
    value.start()
    # The test owns the connection and swaps its fault seam before dispatch.
    value._writer._connection.execute('CREATE TABLE commit_effects (name TEXT PRIMARY KEY, total INTEGER NOT NULL)')
    try:
        yield value
    finally:
        value.close()


def effect(name, calls):
    def apply(session):
        calls.append(name)
        session.execute('INSERT INTO commit_effects(name, total) VALUES (?, 1) '
                        'ON CONFLICT(name) DO UPDATE SET total=total+1', (name,))
        return session.fetch_one('SELECT total FROM commit_effects WHERE name=?', (name,))['total']
    return apply


def command(backend, name, callback, *, receipt):
    return backend.command('test.commit_delivery', hashlib.sha256(name.encode()).hexdigest(),
                           'delivery-'+name if receipt else None, 'user', callback,
                           time.monotonic()+10, receipt_required=receipt)


def totals(backend):
    return backend.query('test.commit_effects', lambda session: session.fetch_all(
        'SELECT name, total FROM commit_effects ORDER BY name'), time.monotonic()+5)


@pytest.mark.parametrize('receipt', [False, True])
@pytest.mark.parametrize('phase', ['before', 'after'])
def test_single_commit_delivery_preserves_effect_and_receipt_boundary(backend, receipt, phase):
    fault = CommitFault(backend._writer._connection, phase=phase)
    backend._writer._connection = fault
    calls = []
    if phase == 'after' and not receipt:
        with pytest.raises(StorageError) as error:
            command(backend, 'one', effect('one', calls), receipt=receipt)
        assert error.value.code == 'database_unavailable'
        assert not error.value.request_not_dispatched
        assert fault.commits == 1
    else:
        assert command(backend, 'one', effect('one', calls), receipt=receipt) == 1
        assert fault.commits == 2
    assert calls == ['one'] * (2 if phase == 'before' else 1)
    assert totals(backend) == [{'name': 'one', 'total': 1}]


@pytest.mark.parametrize('phase', ['before', 'after'])
def test_mixed_batch_retries_only_proven_rollback_or_durable_receipts(backend, phase):
    fault = CommitFault(backend._writer._connection, phase=phase, commit_number=2)
    backend._writer._connection = fault
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocker(_session):
        entered.set()
        assert release.wait(5)
        return 'released'

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(command, backend, 'blocker', blocker, receipt=False)
        assert entered.wait(2)
        unsafe = executor.submit(command, backend, 'one', effect('one', calls), receipt=False)
        safe = executor.submit(command, backend, 'two', effect('two', calls), receipt=True)
        try:
            until = time.monotonic()+5
            while backend._writer.queue_depths()['user'] != 2:
                assert time.monotonic() < until
                time.sleep(.005)
        finally:
            release.set()
        assert first.result(5) == 'released'
        if phase == 'after':
            with pytest.raises(StorageError) as error:
                unsafe.result(5)
            assert error.value.code == 'database_unavailable'
            assert not error.value.request_not_dispatched
        else:
            assert unsafe.result(5) == 1
        assert safe.result(5) == 1
    assert sorted(calls) == ['one'] * (2 if phase == 'before' else 1) + ['two'] * (2 if phase == 'before' else 1)
    assert totals(backend) == [{'name': 'one', 'total': 1}, {'name': 'two', 'total': 1}]


def test_unknown_segment_does_not_fail_or_repeat_later_unexecuted_jobs(backend):
    backend._writer._connection = CommitFault(
        backend._writer._connection, phase='after', commit_number=2)
    # Exercise the writer's supported zero-length segment budget, with a
    # deterministic queue barrier so the next job belongs to a later segment.
    backend._writer._segment_budget_s = 0
    entered, release = threading.Event(), threading.Event()
    calls = []

    def blocker(_session):
        entered.set()
        assert release.wait(5)

    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(command, backend, 'blocker', blocker, receipt=False)
        assert entered.wait(2)
        unknown = executor.submit(command, backend, 'one', effect('one', calls), receipt=False)
        later = None
        try:
            until = time.monotonic()+5
            while backend._writer.queue_depths()['user'] != 1:
                assert time.monotonic() < until
                time.sleep(.005)
            later = executor.submit(command, backend, 'two', effect('two', calls), receipt=False)
            while backend._writer.queue_depths()['user'] != 2:
                assert time.monotonic() < until
                time.sleep(.005)
        finally:
            release.set()
        first.result(5)
        with pytest.raises(StorageError) as error:
            unknown.result(5)
        assert error.value.code == 'database_unavailable'
        assert later.result(5) == 1
    assert calls == ['one', 'two']
    assert totals(backend) == [{'name': 'one', 'total': 1}, {'name': 'two', 'total': 1}]


def test_missing_transaction_state_cannot_authorize_unreceipted_replay(backend):
    class UnknownState(CommitFault):
        @property
        def in_transaction(self):
            raise sqlite3.ProgrammingError('transaction state unavailable')

    backend._writer._connection = UnknownState(
        backend._writer._connection, phase='before')
    calls = []
    with pytest.raises(StorageError):
        command(backend, 'one', effect('one', calls), receipt=False)
    assert calls == ['one']
    assert totals(backend) == []


def test_raw_writer_callback_never_replays_its_unowned_commit(backend):
    calls = []

    def raw(session):
        session.execute('BEGIN IMMEDIATE')
        effect('one', calls)(session)
        session.execute('COMMIT')
        error = sqlite3.OperationalError('raw operation failed after commit')
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        raise error

    with pytest.raises(StorageError) as error:
        backend._writer.submit(raw, 'maintenance', time.monotonic()+5,
                               operation_name='test.raw_commit', raw=True)
    assert error.value.code == 'database_unavailable'
    assert calls == ['one']
    assert totals(backend) == [{'name': 'one', 'total': 1}]

"""Regression coverage for transient SQLite ``disk I/O error`` retry.

Symptom (from production logs, FUSE/DolphinFS deploy)
-----------------------------------------------------
A ``run_task`` SQL write failed hard with::

    sqlite3.OperationalError: disk I/O error

``disk I/O error`` (SQLITE_IOERR) is transient on a FUSE / NFS mount: the
backend store hiccups and ``pread``/``pwrite`` returns EIO. Previously
``db_execute_with_retry`` only treated ``database is locked`` / ``busy`` as
retryable on the SQLite backend, so a single storage blip failed the whole
task. It is now retryable too — a bounded retry rides out the blip while a
genuinely broken mount still exhausts ``max_retries`` and raises.

These tests exercise the PURE retry logic with a lightweight fake db — no
live SQLite file needed.

Run:  pytest tests/test_db_sqlite_ioerr_retry.py -v
"""
from __future__ import annotations

import pytest

import lib.database._core as core
from lib.database._core import db_execute_with_retry


class _FakeSqliteError(Exception):
    """Stand-in for sqlite3.OperationalError (matched by message substring)."""


class _FakeDb:
    """Minimal db wrapper: raises the queued errors, then succeeds."""

    def __init__(self, errors):
        # errors: list of exceptions to raise on successive execute() calls;
        # once exhausted, execute() succeeds.
        self._errors = list(errors)
        self.execute_calls = 0
        self.commit_calls = 0

    def execute(self, sql, params=None):
        self.execute_calls += 1
        if self._errors:
            raise self._errors.pop(0)
        return self

    def commit(self):
        self.commit_calls += 1

    def rollback(self):
        pass


@pytest.fixture(autouse=True)
def _sqlite_backend(monkeypatch):
    monkeypatch.setattr(core, '_BACKEND', 'sqlite')
    # Zero out the backoff sleep so the test is instant.
    monkeypatch.setattr(core.time, 'sleep', lambda *_a, **_k: None)


@pytest.mark.unit
class TestSqliteDiskIoErrorRetry:
    def test_disk_io_error_is_retried_then_succeeds(self):
        """A transient 'disk I/O error' must be retried, not raised."""
        db = _FakeDb([_FakeSqliteError('disk I/O error')])
        db_execute_with_retry(db, 'UPDATE t SET x=1', ())
        assert db.execute_calls == 2, 'should retry once past the transient IOERR'
        assert db.commit_calls == 1

    def test_persistent_disk_io_error_exhausts_and_raises(self):
        """A genuinely broken mount exhausts max_retries and re-raises."""
        db = _FakeDb([_FakeSqliteError('disk I/O error')] * 10)
        with pytest.raises(_FakeSqliteError):
            db_execute_with_retry(db, 'UPDATE t SET x=1', (), max_retries=3)
        assert db.execute_calls == 4, 'initial attempt + 3 retries'

    def test_database_is_locked_still_retryable(self):
        """The pre-existing locked/busy retry path is unchanged."""
        db = _FakeDb([_FakeSqliteError('database is locked')])
        db_execute_with_retry(db, 'UPDATE t SET x=1', ())
        assert db.execute_calls == 2

    def test_non_retryable_sqlite_error_raises_immediately(self):
        """A real logic error (e.g. syntax) must NOT be retried."""
        db = _FakeDb([_FakeSqliteError('no such table: bogus')] * 10)
        with pytest.raises(_FakeSqliteError):
            db_execute_with_retry(db, 'UPDATE bogus SET x=1', ())
        assert db.execute_calls == 1, 'non-retryable error must fail fast'

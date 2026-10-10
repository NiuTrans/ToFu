"""Bounded transient-retry helper for storage-authority stalls."""

from __future__ import annotations

import pytest

from lib.storage.errors import StorageError
from lib.storage.retry import (
    call_with_transient_retry,
    is_transient_storage_error,
)


pytestmark = pytest.mark.unit


class _NeverAbort:
    def wait(self, timeout=None):
        return False

    def is_set(self):
        return False


class _AbortDuringWait:
    def __init__(self):
        self.wait_calls = 0

    def wait(self, timeout=None):
        self.wait_calls += 1
        return True

    def is_set(self):
        return True


def _timeout() -> StorageError:
    # The wire shape the daemon actually emits for deadline_elapsed.
    return StorageError('database_timeout', 'deadline_elapsed', False)


def test_classification_walks_the_cause_chain() -> None:
    assert is_transient_storage_error(_timeout())
    assert is_transient_storage_error(
        StorageError('database_busy', 'rotation backpressure', True))
    assert is_transient_storage_error(
        StorageError('database_unavailable', 'authority unreachable', True))
    wrapped = RuntimeError('event was not durable')
    wrapped.__cause__ = _timeout()
    assert is_transient_storage_error(wrapped)
    assert not is_transient_storage_error(
        StorageError('database_forbidden', 'scope', False))
    assert not is_transient_storage_error(RuntimeError('plain'))
    assert not is_transient_storage_error(None)


def test_success_without_retry() -> None:
    calls = []
    result = call_with_transient_retry(
        lambda: calls.append(1) or 'ok', abort_event=_NeverAbort())
    assert result == 'ok'
    assert calls == [1]


def test_transient_failure_then_success_retries() -> None:
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise _timeout()
        return 'recovered'

    result = call_with_transient_retry(flaky, abort_event=_NeverAbort())
    assert result == 'recovered'
    assert len(calls) == 2


def test_non_transient_failure_raises_immediately() -> None:
    calls = []

    def forbidden():
        calls.append(1)
        raise StorageError('database_forbidden', 'scope', False)

    with pytest.raises(StorageError, match='scope'):
        call_with_transient_retry(forbidden, abort_event=_NeverAbort())
    assert len(calls) == 1


def test_budget_exhaustion_reraises_the_last_error() -> None:
    calls = []

    def always_down():
        calls.append(1)
        raise _timeout()

    with pytest.raises(StorageError, match='deadline_elapsed'):
        call_with_transient_retry(
            always_down, delays=(0.0, 0.0), abort_event=_NeverAbort())
    assert len(calls) == 3


def test_abort_during_backoff_reraises_without_more_attempts() -> None:
    calls = []
    abort = _AbortDuringWait()

    def always_down():
        calls.append(1)
        raise _timeout()

    with pytest.raises(StorageError, match='deadline_elapsed'):
        call_with_transient_retry(always_down, abort_event=abort)
    assert len(calls) == 1
    assert abort.wait_calls == 1


def test_plain_sleep_path_records_the_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    import lib.storage.retry as retry_module
    slept = []
    monkeypatch.setattr(retry_module.time, 'sleep', slept.append)
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise _timeout()
        return 'ok'

    assert call_with_transient_retry(flaky, delays=(1.0, 2.0)) == 'ok'
    assert slept == [1.0, 2.0]

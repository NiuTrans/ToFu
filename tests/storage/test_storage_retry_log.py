"""Rate-limited client retry warnings.

A retrying RPC emits at most one WARNING per operation per throttle
interval (operation, attempt, delay, error class) instead of one line per
attempt — a sustained brownout stays diagnosable without log spam.
"""

from __future__ import annotations

import logging

import pytest

from lib.storage import client as v1_client_mod
from lib.storage import v2_client as v2_module
from lib.storage.errors import StorageError
from lib.storage.retry_log import RetryLogThrottle, retry_log_throttle
from lib.storage.v2_client import StorageV2Client

pytestmark = pytest.mark.unit

_SECRET = b's' * 48


class _FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_throttle_admits_once_per_key_per_interval():
    clock = _FakeClock()
    throttle = RetryLogThrottle(30.0, clock=clock)
    assert throttle.allow('query:conversation.count')
    assert not throttle.allow('query:conversation.count')
    assert not throttle.allow('query:conversation.count')
    # A different operation is throttled independently.
    assert throttle.allow('query:conversation.list')
    clock.now += 29.9
    assert not throttle.allow('query:conversation.count')
    clock.now += 0.2
    assert throttle.allow('query:conversation.count')


def test_throttle_key_cap_is_bounded():
    clock = _FakeClock()
    throttle = RetryLogThrottle(30.0, max_keys=4, clock=clock)
    for index in range(400):
        throttle.allow(f'op-{index}')
    assert len(throttle._last_emitted) <= 4


def test_shared_throttle_identity():
    assert retry_log_throttle() is retry_log_throttle()


class _TimeoutSession:
    outcomes: list = []
    calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    def set_timeout(self, _seconds):
        pass

    def request(self, operation, payload, *, command_id, deadline_unix_ms):
        type(self).calls += 1
        raise StorageError(
            'database_timeout', 'deadline_elapsed', True, 1)

    def close(self):
        pass

    def probe_liveness(self):
        return True


def test_v2_retry_burst_emits_one_warning_per_operation(
        monkeypatch, caplog):
    monkeypatch.setattr(v2_module, 'StorageV2Session', _TimeoutSession)
    # Isolate the process-shared throttle from other tests.
    fresh = RetryLogThrottle(30.0)
    monkeypatch.setattr(v2_module, 'retry_log_throttle', lambda: fresh)
    client = StorageV2Client(
        '127.0.0.1', 1, _SECRET, owner_id=7, tenant_id=3, read_attempts=6,
        timeout=0.05)
    with caplog.at_level(logging.WARNING, logger='lib.storage.v2_client'):
        with pytest.raises(StorageError):
            client.query('conversation.count', {}, deadline=0.05)
    assert _TimeoutSession.calls == 6
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING
                and 'retrying query conversation.count' in r.getMessage()]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert 'attempt 2/6' in message
    assert 'database_timeout' in message
    client.close()


def test_v1_retry_burst_emits_one_warning_per_operation(
        monkeypatch, caplog):
    fresh = RetryLogThrottle(30.0)
    monkeypatch.setattr(v1_client_mod, 'retry_log_throttle', lambda: fresh)
    attempts = []

    class _TimeoutClient(v1_client_mod.StorageClient):
        def _attempt(self, kind, operation, payload, *, timeout,
                     command_id, priority):
            attempts.append(operation)
            raise StorageError(
                'database_timeout', 'request timed out', True, 1)

    client = _TimeoutClient(
        '127.0.0.1', 1, 't' * 40, timeout=0.05, read_attempts=5)
    with caplog.at_level(logging.WARNING, logger='lib.storage.client'):
        with pytest.raises(StorageError):
            client.query('conversation.count', {}, deadline=0.05)
    assert len(attempts) == 5
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING
                and 'retrying query conversation.count' in r.getMessage()]
    assert len(warnings) == 1
    assert 'attempt 2/5' in warnings[0].getMessage()

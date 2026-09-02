"""Read-current authentication and bounded last-used audit writes."""

from __future__ import annotations

import pytest

from lib.storage import StorageError


pytestmark = pytest.mark.unit


def _row(*, last_used_at=None):
    return {
        'id': 'credential-a',
        'name': 'A',
        'scopes': ['chat'],
        'rate_limit_rpm': 60,
        'rate_limit_tpd': 0,
        'owner_user_id': 7,
        'account_user_id': '',
        'tenant_id': '',
        'last_used_at': last_used_at,
    }


class _ReadClient:
    def __init__(self, row):
        self.row = row
        self.calls = []

    def query(self, operation, payload, deadline=None):
        self.calls.append((operation, payload, deadline))
        return self.row


class _WriteClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def command(
        self, operation, payload, command_id, priority='user', deadline=None,
    ):
        self.calls.append(
            (operation, payload, command_id, priority, deadline))
        if self.error is not None:
            raise self.error
        return {'touched': True}


@pytest.fixture
def validation_module(monkeypatch):
    from lib.api_keys import _validate

    _validate._reset_touch_budget_for_test()
    monkeypatch.setattr(_validate, '_TOUCH_INTERVAL_S', 60.0)
    monkeypatch.setattr(_validate, '_TOUCH_RETRY_S', 10.0)
    yield _validate
    _validate._reset_touch_budget_for_test()


def _install_clients(monkeypatch, module, read_client, write_client):
    def get_client(*, write=False):
        return write_client if write else read_client

    monkeypatch.setattr(module, 'get_storage_client', get_client)


def test_every_request_revalidates_but_audit_touch_is_rate_limited(
    monkeypatch, validation_module,
):
    reads = _ReadClient(_row())
    writes = _WriteClient()
    _install_clients(monkeypatch, validation_module, reads, writes)
    monkeypatch.setattr(validation_module.time, 'time', lambda: 1_000.0)
    monotonic_values = iter((100.0, 101.0))
    monkeypatch.setattr(
        validation_module.time, 'monotonic', lambda: next(monotonic_values))

    first = validation_module.validate_token('tofu_live_valid')
    second = validation_module.validate_token('tofu_live_valid')

    assert first is not None and second is not None
    assert [call[0] for call in reads.calls] == [
        'credential.validate', 'credential.validate']
    assert len(writes.calls) == 1
    operation, payload, command_id, priority, deadline = writes.calls[0]
    assert operation == 'credential.touch'
    assert payload['owner_user_id'] == 7
    assert payload['touch_if_before'] == 940.0
    assert command_id is None
    assert priority == 'maintenance'
    assert deadline == 0.25


def test_revocation_is_observed_even_inside_the_touch_interval(
    monkeypatch, validation_module,
):
    reads = _ReadClient(_row())
    writes = _WriteClient()
    _install_clients(monkeypatch, validation_module, reads, writes)
    monkeypatch.setattr(validation_module.time, 'time', lambda: 1_000.0)
    monotonic_values = iter((100.0, 101.0))
    monkeypatch.setattr(
        validation_module.time, 'monotonic', lambda: next(monotonic_values))

    assert validation_module.validate_token('tofu_live_valid') is not None
    reads.row = None
    assert validation_module.validate_token('tofu_live_valid') is None

    assert len(reads.calls) == 2
    assert len(writes.calls) == 1


def test_failed_audit_touch_does_not_deny_or_create_a_retry_storm(
    monkeypatch, validation_module,
):
    reads = _ReadClient(_row())
    writes = _WriteClient(StorageError(
        'database_timeout', 'writer congested', retryable=True))
    _install_clients(monkeypatch, validation_module, reads, writes)
    monkeypatch.setattr(validation_module.time, 'time', lambda: 1_000.0)
    monotonic_values = iter((100.0, 101.0))
    monkeypatch.setattr(
        validation_module.time, 'monotonic', lambda: next(monotonic_values))

    assert validation_module.validate_token('tofu_live_valid') is not None
    assert validation_module.validate_token('tofu_live_valid') is not None

    assert len(reads.calls) == 2
    assert len(writes.calls) == 1


def test_recent_database_touch_suppresses_the_first_process_local_write(
    monkeypatch, validation_module,
):
    reads = _ReadClient(_row(last_used_at=980.0))
    writes = _WriteClient()
    _install_clients(monkeypatch, validation_module, reads, writes)
    monkeypatch.setattr(validation_module.time, 'time', lambda: 1_000.0)
    monkeypatch.setattr(validation_module.time, 'monotonic', lambda: 100.0)

    assert validation_module.validate_token('tofu_live_valid') is not None

    assert len(reads.calls) == 1
    assert writes.calls == []

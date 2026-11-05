"""The managed recovery port preserves scope, budget and failure authority."""
from types import SimpleNamespace

import pytest

from lib.storage import v2_client
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


def client(owner=1):
    return v2_client.StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=owner, tenant_id=1)


@pytest.mark.parametrize('recovers', [True, False])
def test_managed_outage_uses_one_budget_and_receipt(monkeypatch, recovers):
    clock = [100.0]
    monkeypatch.setattr(v2_client, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], time=lambda: clock[0],
        sleep=lambda delay: clock.__setitem__(0, clock[0] + delay)))
    monkeypatch.setattr(v2_client.random, 'uniform', lambda *_: 0)
    original, replacement = client(), client()
    identities, budgets = [], []

    def unavailable(operation, payload, **kwargs):
        identities.append(kwargs['command_id'])
        raise StorageError('database_unavailable', 'restart pending', True, 100)

    def resolve(timeout):
        budgets.append(timeout)
        return replacement if recovers and len(budgets) >= 5 else original

    monkeypatch.setattr(original, '_attempt', unavailable)
    def replay(operation, payload, **kwargs):
        identities.append(kwargs['command_id'])
        return {'version': 1}

    monkeypatch.setattr(replacement, '_attempt', replay)
    original.bind_runtime_recovery(resolve)
    try:
        if recovers:
            assert original.command('record.put', {}, 'one-receipt', deadline=2) == {'version': 1}
            assert len(budgets) == 5
        else:
            with pytest.raises(StorageError, match='restart pending'):
                original.command('record.put', {}, 'one-receipt', deadline=2)
        assert set(identities) == {'one-receipt'}
        assert clock[0] <= 145.001
        assert all(0 < value <= 45 for value in budgets)
        assert budgets == sorted(budgets, reverse=True)
    finally:
        original.close()
        replacement.close()


@pytest.mark.parametrize('replacement_kind', ['foreign_owner', 'foreign_backend', 'stopped'])
def test_recovery_cannot_cross_scope_or_restart_a_stopped_runtime(monkeypatch, replacement_kind):
    original, replacement = client(), client(2)
    seen = []
    failure = StorageError('database_unavailable', 'disconnected', True, 1)
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(original, '_attempt', unavailable)
    monkeypatch.setattr(replacement, '_attempt', lambda *a, **kw: seen.append('dispatched'))

    def resolve(timeout):
        if replacement_kind == 'stopped':
            raise StorageError('database_unavailable', 'recovery stopped', False)
        return replacement if replacement_kind == 'foreign_owner' else object()

    original.bind_runtime_recovery(resolve)
    try:
        with pytest.raises(StorageError) as error:
            original.command('record.put', {}, 'one-receipt', deadline=1)
        assert not error.value.retryable
        assert error.value.code == ('database_unavailable' if replacement_kind == 'stopped'
                                   else 'database_integrity')
        assert not seen
    finally:
        original.close()
        replacement.close()


def test_checkpoint_control_never_uses_generation_recovery(monkeypatch):
    original = client()
    resolutions = []
    original.bind_runtime_recovery(lambda timeout: resolutions.append(timeout))
    failure = StorageError('database_unavailable', 'bound instance closed', True)
    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(original, '_request_once', unavailable)
    try:
        with pytest.raises(StorageError) as error:
            original._checkpoint_request('checkpoint.control', {}, 1, 2**62)
        assert error.value is failure
        assert not resolutions
    finally:
        original.close()

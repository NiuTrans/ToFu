"""Local admission congestion backs off without slowing native WAL recovery."""
import pytest

from lib.storage import v2_client as storage
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("not_dispatched,hint,expected_waits", [
    (True, 25, [0.025, 0.5, 1.0]),
    (True, 50, [0.05, 0.5, 1.0]),
    (False, 50, [0.002, 0.004, 0.008]),
])
def test_busy_command_spaces_local_pressure_and_replays_identity(
        monkeypatch, not_dispatched, hint, expected_waits):
    clock = [100.0]
    waits, commands = [], []
    monkeypatch.setattr(storage.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(storage.random, 'uniform', lambda *_: 0.0)
    def sleep(delay):
        waits.append(delay)
        clock[0] += delay
    monkeypatch.setattr(storage.time, 'sleep', sleep)
    client = storage.StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=1, tenant_id=1)
    def attempt(operation, payload, **kwargs):
        commands.append(kwargs['command_id'])
        if len(commands) <= 3:
            raise StorageError('database_busy', 'admission congestion', True,
                               hint, request_not_dispatched=not_dispatched)
        return {'applied': True}
    monkeypatch.setattr(client, '_attempt', attempt)
    try:
        assert client.command('record.put', {}, 'same-command', deadline=20) == {
            'applied': True}
        assert waits == pytest.approx(expected_waits)
        assert commands == ['same-command'] * 4
    finally:
        client.close()


def test_long_lane_rejection_proves_no_request_was_dispatched():
    pool = storage._SessionPool(lambda timeout: object(), 4)
    held = pool.acquire(60)
    try:
        with pytest.raises(StorageError) as caught:
            pool.acquire(60)
        assert caught.value.code == 'database_busy'
        assert caught.value.request_not_dispatched is True
        assert caught.value.retry_after_ms == 50
        assert pool.resource_snapshot()['used'] == 1
    finally:
        pool.release(held, broken=False)
        pool.close()

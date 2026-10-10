"""Restart recovery drains thousands of attempts without sweeping new work."""
import pytest

from lib import turn_lifecycle
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


def test_recovery_drains_more_than_sixty_four_bounded_transactions(monkeypatch):
    class Client:
        remaining = 2048
        calls = []
        def command(self, operation, payload, command_id, *, deadline):
            self.calls.append((operation, dict(payload), command_id))
            self.remaining -= 8
            return {'recovered': 8, 'remaining': self.remaining}
    client = Client()
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **_: client)
    monkeypatch.setattr(turn_lifecycle, '_now_ms', lambda: 12345)
    assert turn_lifecycle.recover_running_attempts(exclude_task_ids=['live']) == 2048
    assert len(client.calls) == 256
    assert len({call[2] for call in client.calls}) == 256
    assert all(call[1] == {'created_before_ms': 12345, 'exclude_task_ids': ['live']}
               for call in client.calls)


def test_no_progress_is_an_explicit_retryable_failure(monkeypatch):
    class Client:
        def command(self, *_args, **_kwargs):
            return {'recovered': 0, 'remaining': 1}
    monkeypatch.setattr(turn_lifecycle, '_turn_client', lambda **_: Client())
    with pytest.raises(StorageError) as error:
        turn_lifecycle.recover_running_attempts()
    assert error.value.retryable and error.value.code == 'database_busy'

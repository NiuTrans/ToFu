"""Interrupted exchanges retire sockets without swallowing the original cause."""
import pytest

from lib.storage import v2_client
from lib.storage.errors import StorageError

pytestmark = pytest.mark.unit


class Cancelled(BaseException):
    pass


@pytest.mark.parametrize('lane', ['query', 'health'])
@pytest.mark.parametrize('failure', [Cancelled, RuntimeError, StorageError])
def test_interrupted_exchange_cannot_reenter_the_idle_pool(monkeypatch, lane, failure):
    sessions = []
    injected = (StorageError('database_internal', 'interrupted frame')
                if failure is StorageError else failure('interrupted frame'))

    class Response:
        status = 0

        def json(self):
            return {'ready': True, 'count': 1}

    class Session:
        def __init__(self, *args, **kwargs):
            self.closed = False
            sessions.append(self)

        def close(self):
            self.closed = True

        def request(self, *args, **kwargs):
            if len(sessions) == 1:
                raise injected
            return Response()

    monkeypatch.setattr(v2_client, 'StorageV2Session', Session)
    client = v2_client.StorageV2Client('127.0.0.1', 12345, b's' * 48, owner_id=7, tenant_id=3)
    call = (lambda: client.query('conversation.count', {})) if lane == 'query' else (
        lambda: client._health_call('system.health', {}, deadline=1))
    try:
        with pytest.raises(failure) as error:
            call()
        assert error.value is injected
        assert sessions[0].closed, 'partially consumed response socket was retained'
        assert call()['count'] == 1
        assert len(sessions) == 2
    finally:
        client.close()


@pytest.mark.parametrize('failure', [OSError, Cancelled])
def test_timeout_setup_failure_closes_unpublished_session(monkeypatch, failure):
    sessions = []
    injected = failure('timeout setup interrupted')

    class Session:
        closed = False

        def __init__(self, *args, **kwargs):
            sessions.append(self)

        def set_timeout(self, timeout):
            raise injected

        def close(self):
            self.closed = True

    monkeypatch.setattr(v2_client, 'StorageV2Session', Session)
    client = v2_client.StorageV2Client('127.0.0.1', 12345, b's' * 48, owner_id=7, tenant_id=3)
    try:
        with pytest.raises(failure) as error:
            client._open_session(1)
        assert error.value is injected
        assert len(sessions) == 1 and sessions[0].closed
    finally:
        client.close()


def test_closed_client_health_cannot_open_a_new_session(monkeypatch):
    client = v2_client.StorageV2Client('127.0.0.1', 12345, b's' * 48, owner_id=7, tenant_id=3)
    calls = []
    monkeypatch.setattr(client, '_open_session', lambda timeout: calls.append(timeout))
    client.close()
    with pytest.raises(StorageError) as error:
        client._health_call('system.health', {}, deadline=1)
    assert error.value.code == 'database_unavailable'
    assert not calls

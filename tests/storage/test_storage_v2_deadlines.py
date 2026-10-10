"""Queueing and handshake time consume the storage request's own deadline."""
import threading
import time

import pytest

from lib.storage.errors import StorageError
from lib.storage.v2_client import StorageV2Client, _SessionPool

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('recovers', [True, False])
def test_busy_rotation_uses_existing_deadline_and_preserves_command_identity(monkeypatch, recovers):
    clock = [100.0]
    identities = []
    client = StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=1, tenant_id=1)
    monkeypatch.setattr('lib.storage.v2_client.time.monotonic', lambda: clock[0])
    monkeypatch.setattr('lib.storage.v2_client.time.sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr('lib.storage.v2_client.random.uniform', lambda *_: 0)
    def attempt(_operation, _payload, **kwargs):
        identities.append(kwargs['command_id'])
        if recovers and len(identities) > 20:
            return {'recovered': 8}
        raise StorageError('database_busy', 'rotation is draining', True, 50)
    monkeypatch.setattr(client, '_attempt', attempt)
    try:
        if recovers:
            assert client.command('turn.recover', {}, 'recovery-chunk', deadline=2) == {'recovered': 8}
            assert len(identities) == 21
        else:
            with pytest.raises(StorageError, match='rotation is draining'):
                client.command('turn.recover', {}, 'recovery-chunk', deadline=2)
            assert clock[0] <= 145.001  # existing 45 s command aggregate budget
        assert set(identities) == {'recovery-chunk'}
    finally:
        client.close()


def test_health_waiter_expires_without_waiting_for_a_stuck_probe():
    client = StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=1, tenant_id=1)
    client._health_lock.acquire()
    result = []
    probe = threading.Thread(target=lambda: result.append(client.health(deadline=.025)))
    try:
        probe.start()
        probe.join(timeout=1)
        assert not probe.is_alive()
        assert result == [dict(ready=False, backend='tofudb', protocol='storage.v2',
                               owner_id=1, tenant_id=1, probe_error='database_timeout')]
    finally:
        client._health_lock.release()
        probe.join(timeout=1)
        client.close()


def test_pool_hands_connection_factory_only_remaining_budget(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('lib.storage.v2_client.time.monotonic', lambda: now[0])
    budgets = []
    class Session:
        def probe_liveness(self):
            now[0] += .05
            return False
        def close(self):
            pass
    pool = _SessionPool(lambda timeout: (budgets.append(timeout), Session())[1], 1)
    session = pool.acquire(.5)
    pool.release(session, broken=False)
    try:
        replacement = pool.acquire(.2)
        assert budgets[-1] == pytest.approx(.15)
        pool.release(replacement, broken=False)
        assert pool.resource_snapshot()['used'] == 0
    finally:
        pool.close()


def test_expired_connection_does_not_dispatch_and_returns_pool_slot(monkeypatch):
    class Session:
        requests = 0
        def request(self, *_args, **_kwargs):
            self.requests += 1

    clock = [100.0]
    session = Session()
    client = StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=1, tenant_id=1)
    def connect(_timeout):
        clock[0] += 2
        return session
    monkeypatch.setattr('lib.storage.v2_client.time.monotonic', lambda: clock[0])
    client._pool = _SessionPool(connect, 1)
    try:
        with pytest.raises(StorageError) as error:
            client._attempt('conversation.count', {}, timeout=1,
                            command_id=None, deadline_unix_ms=101000)
        assert error.value.code == 'database_timeout'
        assert error.value.request_not_dispatched
        assert session.requests == 0
        assert client.resource_snapshot()['used'] == 0
    finally:
        client.close()


def test_reused_health_session_uses_each_probes_budget():
    class Session:
        timeouts = []
        def set_timeout(self, timeout):
            self.timeouts.append(timeout)
        def request(self, *_args, **_kwargs):
            return type('Response', (), {'status': 0, 'json': lambda _: {'ready': True}})()

    session = Session()
    client = StorageV2Client('127.0.0.1', 1, b's' * 48, owner_id=1, tenant_id=1)
    client._health_session = session
    try:
        assert client.health(deadline=2)['ready']
        assert client.health(deadline=.1)['ready']
        assert 0 < session.timeouts[1] <= .1 < session.timeouts[0] <= 2
    finally:
        client.close()


def test_busy_sessions_do_not_consume_other_callers_attempt_deadline():
    pool = _SessionPool(lambda _timeout: object(), 1)
    held = [pool.acquire(180) for _ in range(8)]
    short = pool.acquire(.02)
    assert short not in held
    assert pool.resource_snapshot()['waiting'] == 0
    for session in [*held, short]:
        pool.release(session, broken=False)
    assert pool.resource_snapshot()['used'] == 0
    pool.close()

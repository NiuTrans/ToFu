"""Daemon checkpoint transport keeps instance identity and never replays commands."""
import time
from collections import deque
from types import SimpleNamespace

import pytest

from lib.storage import v2_client
from lib.storage.errors import StorageError
from tofu_db.generated_checkpoint_control import (
    CAPABILITIES_OPERATION, CONTROL_OPERATION, FORMAT, TRANSPORT_FORMAT,
)

pytestmark = pytest.mark.unit
RUNTIME = '550e8400-e29b-41d4-a716-446655440000'


@pytest.fixture
def transport(monkeypatch):
    state = SimpleNamespace(replies=deque(), calls=[], closed=0)
    class Session:
        def __init__(self, *args, **kwargs): pass
        def set_timeout(self, seconds): assert seconds > 0
        def probe_liveness(self): return True
        def close(self): state.closed += 1
        def request(self, operation, payload, **options):
            state.calls.append((operation, payload, options))
            reply = state.replies.popleft()
            if isinstance(reply, BaseException): raise reply
            return SimpleNamespace(status=0, json=lambda: reply)
    monkeypatch.setattr(v2_client, 'StorageV2Session', Session)
    client = v2_client.StorageV2Client('127.0.0.1', 1234, b's' * 48,
                                      owner_id=7, tenant_id=3)
    yield client, state
    client.close()


def capabilities(**changes):
    return dict(dict(format=TRANSPORT_FORMAT, available=True, runtime_id=RUNTIME,
                     maximum_response_bytes=8384512), **changes)


def call(client, **changes):
    options = dict(user_id=7, deadline_unix_ms=int((time.time() + 5) * 1000),
                   response_bytes=8192)
    options.update(changes)
    return client.native_checkpoint_control('load', dict(format=FORMAT, capability_id=1), **options)


def test_checkpoint_negotiation_is_cached_and_commands_keep_instance_binding(transport):
    client, state = transport
    state.replies.extend([capabilities(), dict(format=FORMAT, accepted=True),
                          dict(format=FORMAT, accepted=False, error='conflict')])
    assert client.native_checkpoint_available(user_id=7)
    assert call(client)['accepted']
    assert not call(client)['accepted']
    assert [row[0] for row in state.calls] == [CAPABILITIES_OPERATION, CONTROL_OPERATION, CONTROL_OPERATION]
    assert state.calls[0][1] == {}
    for _, payload, options in state.calls[1:]:
        assert payload['runtime_id'] == RUNTIME
        assert payload['payload']['capability_id'] == 1
        assert 'user_id' not in payload and options['command_id'] is None


@pytest.mark.parametrize('owner', [True, 8, '7'])
def test_checkpoint_foreign_owner_never_probes_or_dispatches(transport, owner):
    client, state = transport
    with pytest.raises(StorageError, match='owner differs') as caught: call(client, user_id=owner)
    assert caught.value.code == 'database_forbidden'
    assert not state.calls


@pytest.mark.parametrize('changes', [dict(response_bytes=0), dict(response_bytes=True),
                                    dict(deadline_unix_ms=1), dict(deadline_unix_ms=1 << 64)])
def test_checkpoint_invalid_or_expired_admission_never_dispatches(transport, changes):
    client, state = transport
    with pytest.raises(StorageError): call(client, **changes)
    assert not state.calls


@pytest.mark.parametrize('changes', [dict(runtime_id='old'), dict(available=1),
                                    dict(maximum_response_bytes=True), dict(format='wrong')])
def test_checkpoint_invalid_capability_response_never_grants_a_command(transport, changes):
    client, state = transport
    state.replies.append(capabilities(**changes))
    with pytest.raises(StorageError, match='invalid checkpoint'): call(client)
    assert len(state.calls) == 1


def test_checkpoint_unknown_delivery_retires_session_without_replay_or_rebinding(transport):
    client, state = transport
    state.replies.extend([capabilities(), OSError('response lost')])
    with pytest.raises(StorageError): call(client)
    assert len(state.calls) == 2 and state.closed == 1
    assert client.native_checkpoint_available(user_id=7)
    assert len(state.calls) == 2


def test_checkpoint_unsupported_and_closed_clients_cannot_dispatch(transport):
    client, state = transport
    state.replies.append(capabilities(available=False))
    assert not client.native_checkpoint_available(user_id=7)
    with pytest.raises(StorageError, match='lacks durable'): call(client)
    client.close()
    with pytest.raises(StorageError, match='closed'): client.native_checkpoint_available(user_id=7)
    assert len(state.calls) == 1

"""Unit tests for the application-facing storage.v2 client adapter.

A fake session replaces the network transport so error mapping, payload owner
injection, retry, and the committed-event unwrap are exercised without a live
tofu-db daemon.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lib.storage import v2_client as v2_module
from lib.storage.errors import StorageError
from lib.storage.v2_client import StorageV2Client
from lib.storage import commit_events as commit_events_module

pytestmark = pytest.mark.unit

_SECRET = b's' * 48


class _FakeResponse:
    def __init__(self, status: int, payload: bytes | bytearray = b'',
                 error_code: str | None = None,
                 error_message: str | None = None,
                 retryable: bool = False):
        self.status = status
        self.payload = payload
        self.error_code = error_code
        self.error_message = error_message
        self.retryable = retryable

    def json(self) -> Any:
        if not self.payload:
            return None
        return json.loads(bytes(self.payload).decode('utf-8'))


class _FakeSession:
    """Scripted session; records the body it was asked to send.

    Deliberately omits ``close``/``set_timeout``/``probe_liveness``: pool and
    health-lane lifecycle code tolerates their absence (``getattr``/
    ``_close_quietly``), and several tests here depend on the no-op surface.
    A dropped session has no close side effect, so a garbage-collected lane
    session cannot perturb another suite's class counters."""

    responses: list = []
    seen: list[dict[str, Any]] = []
    calls: int = 0
    constructed: int = 0

    def __init__(self, *args, **kwargs):
        type(self).constructed += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def request(self, operation, payload, *, command_id, deadline_unix_ms):
        type(self).calls += 1
        type(self).seen.append({
            'operation': operation, 'payload': dict(payload),
            'command_id': command_id,
        })
        outcome = type(self).responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch):
    _FakeSession.responses = []
    _FakeSession.seen = []
    _FakeSession.calls = 0
    _FakeSession.constructed = 0
    monkeypatch.setattr(
        v2_module, 'StorageV2Session', _FakeSession)
    yield


def _client() -> StorageV2Client:
    return StorageV2Client(
        '127.0.0.1', 4000, _SECRET, owner_id=7, tenant_id=3)


def test_constructor_validation() -> None:
    with pytest.raises(ValueError):
        StorageV2Client('8.8.8.8', 4000, _SECRET, owner_id=1, tenant_id=1)
    with pytest.raises(ValueError):
        StorageV2Client('127.0.0.1', 4000, b'short', owner_id=1, tenant_id=1)
    with pytest.raises(ValueError):
        StorageV2Client('127.0.0.1', 4000, _SECRET, owner_id=0, tenant_id=1)


def test_query_returns_result_and_injects_owner() -> None:
    _FakeSession.responses = [_FakeResponse(0, json.dumps({'count': 4}).encode())]
    result = _client().query('conversation.count', {}, deadline=5.0)
    assert result == {'count': 4}
    assert _FakeSession.seen[0]['payload']['user_id'] == 7
    assert _FakeSession.seen[0]['command_id'] is None


def test_query_does_not_overwrite_explicit_owner() -> None:
    _FakeSession.responses = [_FakeResponse(0, b'{}')]
    _client().query('conversation.count', {'user_id': 9}, deadline=5.0)
    assert _FakeSession.seen[0]['payload']['user_id'] == 9


def test_command_maps_v2_error_codes() -> None:
    _FakeSession.responses = [
        _FakeResponse(400, b'', 'authority_scope_mismatch', 'scope', False)]
    with pytest.raises(StorageError) as excinfo:
        _client().command(
            'conversation.create', {'user_id': 7}, 'cid-1', deadline=5.0)
    assert excinfo.value.code == 'database_forbidden'


def test_deadline_elapsed_maps_to_timeout_and_retries() -> None:
    _FakeSession.responses = [
        _FakeResponse(408, b'', 'deadline_elapsed', 'slow', True),
        _FakeResponse(0, json.dumps({'ok': True}).encode()),
    ]
    result = _client().query('conversation.count', {}, deadline=5.0)
    assert result == {'ok': True}
    # One transient failure then a successful retry.
    assert _FakeSession.calls == 2


def test_deadline_elapsed_wire_non_retryable_read_still_retries() -> None:
    # The real daemon emits deadline_elapsed with retryable=False: the
    # admission variant is a pre-dispatch rejection and the execution
    # variant aborts pre-commit, so a read replay is always safe.
    _FakeSession.responses = [
        _FakeResponse(408, b'', 'deadline_elapsed', 'deadline_elapsed', False),
        _FakeResponse(0, json.dumps({'ok': True}).encode()),
    ]
    result = _client().query('conversation.count', {}, deadline=5.0)
    assert result == {'ok': True}
    assert _FakeSession.calls == 2


def test_deadline_elapsed_wire_non_retryable_command_retries_same_command_id() -> None:
    # Committed commands leave a durable (tenant, owner, command_id)
    # receipt server-side, so a same-command-id replay after a timeout can
    # never double-apply — a transient authority stall degrades to a wait.
    _FakeSession.responses = [
        _FakeResponse(408, b'', 'deadline_elapsed', 'deadline_elapsed', False),
        _FakeResponse(0, json.dumps({'ok': True}).encode()),
    ]
    result = _client().command(
        'record.put', {'namespace': 'n', 'key': 'k'}, 'cid-stable',
        deadline=5.0)
    assert result == {'ok': True}
    assert _FakeSession.calls == 2
    assert [row['command_id'] for row in _FakeSession.seen] == [
        'cid-stable', 'cid-stable']


def test_command_unavailable_replays_same_command_id() -> None:
    # A transport refusal before the first response byte is replay-safe for
    # commands by the same receipt proof as a peer close at a frame
    # boundary: either the command never reached the authority, or it
    # committed and left a durable (tenant, owner, command_id) receipt
    # whose replay returns the recorded outcome. The supervised-restart
    # window therefore degrades to a wait instead of a 503 (2026-09-24).
    _FakeSession.responses = [
        OSError('connection refused'),
        OSError('connection reset by peer'),
        _FakeResponse(0, json.dumps({'applied': True}).encode()),
    ]
    result = _client().command(
        'record.put', {'namespace': 'n', 'key': 'k'}, 'cid-x', deadline=5.0)
    assert result == {'applied': True}
    assert _FakeSession.calls == 3
    assert [row['command_id'] for row in _FakeSession.seen] == [
        'cid-x', 'cid-x', 'cid-x']


def test_command_unavailable_exhaustion_still_raises_classified() -> None:
    # Replaying does not mask a persistent outage: when the authority stays
    # unreachable through the busy-command attempt cap the caller still
    # gets the typed database_unavailable error, just after a bounded wait.
    _FakeSession.responses = [OSError('connection refused')] * 16
    with pytest.raises(StorageError) as excinfo:
        _client().command(
            'record.put', {'namespace': 'n', 'key': 'k'}, 'cid-x', deadline=5.0)
    assert excinfo.value.code == 'database_unavailable'
    assert excinfo.value.retryable is True
    # Non-busy unavailability exhausts at read_attempts (default 3); the
    # deeper 16-attempt loop bound is reserved for database_busy flow
    # control.
    assert _FakeSession.calls == 3
    assert [row['command_id'] for row in _FakeSession.seen] == [
        'cid-x'] * 3


def test_non_transient_read_error_is_not_retried() -> None:
    _FakeSession.responses = [
        _FakeResponse(403, b'', 'authority_scope_mismatch', 'scope', False)]
    with pytest.raises(StorageError) as excinfo:
        _client().query('conversation.count', {}, deadline=5.0)
    assert excinfo.value.code == 'database_forbidden'
    assert _FakeSession.calls == 1


def test_command_unwraps_committed_envelope_and_publishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published: list = []
    monkeypatch.setattr(
        commit_events_module, 'publish_committed_events',
        lambda events: published.append(list(events)))
    # v2_client imported the symbol at module load; patch that binding too.
    monkeypatch.setattr(
        v2_module, 'publish_committed_events',
        lambda events: published.append(list(events)))
    envelope = {
        '_storageCommitContract': 'storage.committed-events/v1',
        'events': [{'type': 'turn', 'id': 1}],
        'value': {'applied': True},
    }
    _FakeSession.responses = [
        _FakeResponse(0, json.dumps(envelope).encode())]
    result = _client().command(
        'conversation.create', {'user_id': 7}, 'cid-9', deadline=5.0)
    assert result == {'applied': True}
    assert published == [[{'type': 'turn', 'id': 1}]]


def test_command_rejects_unknown_commit_contract() -> None:
    envelope = {
        '_storageCommitContract': 'storage.committed-events/v2',
        'events': [],
        'value': None,
    }
    _FakeSession.responses = [
        _FakeResponse(0, json.dumps(envelope).encode())]
    with pytest.raises(StorageError) as excinfo:
        _client().command(
            'conversation.create', {'user_id': 7}, 'cid-3', deadline=5.0)
    assert excinfo.value.code == 'database_protocol_error'


def test_protocol_violation_maps_to_protocol_error() -> None:
    from tofu_db.wire import StorageV2ProtocolError
    _FakeSession.responses = [
        StorageV2ProtocolError('checksum mismatch')]
    with pytest.raises(StorageError) as excinfo:
        _client().query('conversation.count', {}, deadline=5.0)
    assert excinfo.value.code == 'database_protocol_error'


def test_transport_failure_maps_to_unavailable_and_retries() -> None:
    _FakeSession.responses = [
        OSError('connection refused'),
        _FakeResponse(0, json.dumps({'count': 2}).encode()),
    ]
    result = _client().query('conversation.count', {}, deadline=5.0)
    assert result == {'count': 2}
    assert _FakeSession.calls == 2


def test_maintenance_sends_generated_command_id() -> None:
    """Maintenance-kind ops mutate state, so tofu-db requires a command_id.

    Regression: the client used to forward command_id only for command-kind
    calls, so maintenance ops (e.g. project_brain.recovery.snapshot during
    startup recovery) were rejected with command_id_required and the tofudb
    backend could not boot.
    """
    _FakeSession.responses = [_FakeResponse(0, json.dumps({'projects': []}).encode())]
    result = _client().maintenance(
        'project_brain.recovery.snapshot', {}, deadline=5.0)
    assert result == {'projects': []}
    sent = _FakeSession.seen[0]['command_id']
    assert isinstance(sent, str) and sent
    assert sent.startswith('project_brain.recovery.snapshot:')


def test_command_uses_caller_supplied_command_id() -> None:
    _FakeSession.responses = [_FakeResponse(0, json.dumps({'ok': True}).encode())]
    _client().command('conversation.create', {'user_id': 7}, 'pinned-id', deadline=5.0)
    assert _FakeSession.seen[0]['command_id'] == 'pinned-id'


def test_query_never_sends_command_id() -> None:
    _FakeSession.responses = [_FakeResponse(0, b'{}')]
    _client().query('conversation.count', {}, deadline=5.0)
    assert _FakeSession.seen[0]['command_id'] is None


def test_system_schema_query_keeps_its_empty_payload_contract() -> None:
    _FakeSession.responses = [_FakeResponse(0, b'{"version":62}')]
    _client().query('system.schema_version', {})
    assert _FakeSession.seen[0]['payload'] == {}


def test_system_schema_query_does_not_silently_discard_invalid_fields() -> None:
    _FakeSession.responses = [_FakeResponse(400, b'', 'database_protocol_error', 'invalid', False)]
    with pytest.raises(StorageError):
        _client().query('system.schema_version', {'user_id': 9})
    assert _FakeSession.seen[0]['payload'] == {'user_id': 9}


def test_error_response_stamps_the_operation_on_the_storage_error() -> None:
    """Ops logs render StorageError.operation_id; without the stamp a bare
    database_protocol_error cannot be tied to the rejected operation."""
    _FakeSession.responses = [
        _FakeResponse(400, b'', 'database_protocol_error',
                      'database_protocol_error', False)]
    with pytest.raises(StorageError) as excinfo:
        _client().command('turn.event.record', {'x': 1}, 'cid-op', deadline=5.0)
    assert excinfo.value.code == 'database_protocol_error'
    assert excinfo.value.operation_id == 'turn.event.record'


def test_native_notice_is_mapped_to_owned_conversation_wakeup(monkeypatch):
    published = []
    monkeypatch.setattr(v2_module, 'publish_committed_events', published.extend)
    envelope = {'_storageCommitContract': 'storage.committed-events/v1', 'value': {'applied': True},
                'events': [{'contract': 'storage.commit-notice/v1', 'topic': 'conversation',
                            'userId': 7, 'key': 'owned-conversation', 'sequence': 42}]}
    _FakeSession.responses = [_FakeResponse(0, json.dumps(envelope).encode())]
    assert _client().command('turn.event.record', {}, 'notice-command') == {'applied': True}
    assert published == [{'contract': 'storage.conversation-commit/v1', 'userId': 7,
                          'event': {'conversationId': 'owned-conversation', 'syncSeq': 42}}]


@pytest.mark.parametrize('change', ['missing_value', 'oversized_notices', 'invalid_owner', 'foreign_owner', 'oversized_key', 'invalid_utf8'])
def test_malformed_native_notice_never_reaches_subscribers(monkeypatch, change):
    published = []
    monkeypatch.setattr(v2_module, 'publish_committed_events', published.extend)
    envelope = {'_storageCommitContract': 'storage.committed-events/v1', 'value': True,
                'events': [{'contract': 'storage.commit-notice/v1', 'topic': 'conversation',
                            'userId': 7, 'key': 'owned', 'sequence': 1}]}
    if change == 'missing_value':
        envelope.pop('value')
    elif change == 'oversized_notices':
        envelope['events'] *= v2_module.MAX_COMMIT_NOTICE_KEYS + 1
    elif change == 'foreign_owner':
        envelope['events'][0]['userId'] = 8
    elif change == 'oversized_key':
        envelope['events'][0]['key'] = 'x' * 2049
    elif change == 'invalid_utf8':
        envelope['events'][0]['key'] = '\ud800'
    else:
        envelope['events'][0]['userId'] = True
    _FakeSession.responses = [_FakeResponse(0, json.dumps(envelope).encode())]
    with pytest.raises(StorageError):
        _client().command('turn.event.record', {}, 'bad-notice-command')
    assert not published


def test_system_query_preserves_payload_free_contract() -> None:
    _FakeSession.responses = [_FakeResponse(0, b'{"version":62}')]
    assert _client().query('system.schema_version', {}) == {'version': 62}
    assert _FakeSession.seen[0]['payload'] == {}


@pytest.mark.parametrize('limit', [1, 500, 5000])
def test_cleanup_translates_bounded_progress(limit) -> None:
    from lib.storage.generated_v2_semantics import BOUNDED_PROGRESS_OPERATIONS
    maximum = BOUNDED_PROGRESS_OPERATIONS['turn.cleanup']['nativeMaximumLimit']
    _FakeSession.responses = [_FakeResponse(0, b'{"deletedAttempts":1,"scannedIndexEntries":1}')]
    assert _client().command('turn.cleanup', {'limit': limit}, 'cleanup') == 1
    assert _FakeSession.seen[0]['payload']['limit'] == min(limit, maximum)


@pytest.mark.parametrize('result', [{}, {'deletedAttempts': True}, {'deletedAttempts': -1}])
def test_cleanup_rejects_malformed_progress(result) -> None:
    _FakeSession.responses = [_FakeResponse(0, json.dumps(result).encode())]
    with pytest.raises(StorageError, match='Malformed turn.cleanup'):
        _client().command('turn.cleanup', {}, 'cleanup')


def test_generic_commit_notices_translate_after_progress_unwrap(monkeypatch) -> None:
    published = []
    monkeypatch.setattr(v2_module, 'publish_committed_events', published.extend)
    _FakeSession.responses = [_FakeResponse(0, json.dumps({
        '_storageCommitContract': 'storage.committed-events/v1',
        'value': {'deletedAttempts': 1},
        'events': [{'contract': 'storage.commit-notice/v1', 'topic': 'conversation',
                    'key': 'conv', 'userId': 7, 'sequence': 9}],
    }).encode())]
    assert _client().command('turn.cleanup', {}, 'cleanup') == 1
    assert published == [{'contract': 'storage.conversation-commit/v1', 'userId': 7,
                          'event': {'conversationId': 'conv', 'syncSeq': 9}}]


def test_maintenance_does_not_replay_ambiguous_mutation() -> None:
    _FakeSession.responses = [
        OSError('connection lost after dispatch'),
        _FakeResponse(0, b'{}'),
    ]
    with pytest.raises(StorageError):
        _client().maintenance('project_brain.recovery.snapshot', {})
    assert _FakeSession.calls == 1


@pytest.mark.parametrize('limit', [True, 0, -1, 5001, '64'])
def test_cleanup_rejects_invalid_limit_before_dispatch(limit) -> None:
    with pytest.raises(StorageError, match='Invalid turn.cleanup limit'):
        _client().command('turn.cleanup', {'limit': limit}, 'cleanup')
    assert _FakeSession.calls == 0


def test_slow_read_retry_receives_a_fresh_wire_deadline(monkeypatch):
    clock = [100.0]
    deadlines = []
    monkeypatch.setattr(v2_module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(v2_module.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(v2_module.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(v2_module.random, 'uniform', lambda *_: 0.0)

    def request(self, operation, payload, *, command_id, deadline_unix_ms):
        deadlines.append(deadline_unix_ms)
        if len(deadlines) == 1:
            clock[0] += 5.0
            raise OSError('response lost after slow read')
        assert deadline_unix_ms > int(clock[0] * 1000)
        return _FakeResponse(0, b'{"count":19}')

    monkeypatch.setattr(_FakeSession, 'request', request)
    assert _client().query('conversation.count', {}, deadline=5.0) == {'count': 19}
    assert len(deadlines) == 2
    assert deadlines[1] > deadlines[0]


def test_availability_retries_escalate_beyond_the_healthy_blip_hint(
        monkeypatch):
    """2026-09-22 cgroup memory-pressure incident: a fleet of callers
    re-attempting a starved authority at the flat 50–100 ms hint is the
    storm that keeps it starved. The first retry keeps the server hint
    (a healthy blip pays nothing); consecutive availability failures
    space out exponentially inside the same aggregate budget."""
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(v2_module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(v2_module.time, 'monotonic', lambda: clock[0])

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(v2_module.time, 'sleep', fake_sleep)
    monkeypatch.setattr(v2_module.random, 'uniform', lambda *_: 0.0)
    # TimeoutError maps to database_unavailable with retry_after_ms=100.
    _FakeSession.responses = [
        TimeoutError('authority starved'),
        TimeoutError('authority starved'),
        _FakeResponse(0, b'{"ok": true}'),
    ]
    assert _client().query('conversation.count', {}, deadline=30.0) == {'ok': True}
    assert sleeps == [0.1, 0.5]


def test_resource_exhausted_backs_off_from_first_retry(monkeypatch) -> None:
    """``database_resource_exhausted`` is load-shed, not a blip.

    A generic unavailable blip keeps the cheap 50 ms hint on its first
    retry (a healthy authority pays nothing). But resource_exhausted means
    the authority is actively shedding load — retrying 50 ms later just
    re-hammers it (the 2026-09-24 turn.sync.changes storm). The FIRST retry
    must already back off at the availability base.
    """
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr(v2_module.time, 'time', lambda: clock[0])
    monkeypatch.setattr(v2_module.time, 'monotonic', lambda: clock[0])

    def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr(v2_module.time, 'sleep', fake_sleep)
    monkeypatch.setattr(v2_module.random, 'uniform', lambda *_: 0.0)
    _FakeSession.responses = [
        _FakeResponse(
            429, b'', 'database_resource_exhausted', 'shedding', True),
        _FakeResponse(0, b'{"ok": true}'),
    ]
    assert _client().query(
        'conversation.count', {}, deadline=30.0) == {'ok': True}
    # First retry already at the availability base, not the 50 ms blip hint.
    assert sleeps == [v2_module._AVAILABILITY_BACKOFF_BASE_S]


# ── Persistent session pool ──────────────────────────────────────────────
def test_pool_amortizes_session_setup_across_rpcs() -> None:
    _FakeSession.responses = [_FakeResponse(0, b'{}')] * 6
    client = _client()
    for _ in range(6):
        client.query('conversation.count', {}, deadline=5.0)
    assert _FakeSession.constructed == 1


def test_pool_retires_broken_session_and_retry_builds_fresh() -> None:
    _FakeSession.responses = [
        TimeoutError('wedged'),
        _FakeResponse(0, json.dumps({'ok': True}).encode()),
    ]
    result = _client().query('conversation.count', {}, deadline=5.0)
    assert result == {'ok': True}
    assert _FakeSession.constructed == 2


def test_pool_healthy_session_survives_application_error_responses() -> None:
    _FakeSession.responses = [
        _FakeResponse(400, b'', 'database_not_found', 'missing', False),
        _FakeResponse(0, b'{}'),
    ]
    client = _client()
    with pytest.raises(StorageError):
        client.query('conversation.count', {}, deadline=5.0)
    client.query('conversation.count', {}, deadline=5.0)
    assert _FakeSession.constructed == 1


@pytest.mark.parametrize('timeout', [.1, 180.0])
def test_pool_creates_active_sessions_beyond_idle_target(timeout):
    from lib.storage.v2_client import _SessionPool
    pool = _SessionPool(lambda _timeout: object(), 1)
    held = [pool.acquire(timeout) for _ in range(12)]
    assert len({id(session) for session in held}) == 12
    assert pool.resource_snapshot()['used'] == 12
    assert pool.resource_snapshot()['waiting'] == 0
    for session in held:
        pool.release(session, broken=False)
    assert pool.resource_snapshot()['idle'] == 1
    assert pool.resource_snapshot()['used'] == 0
    pool.close()


def test_pool_resize_changes_idle_retention_without_refusing_active_calls():
    from lib.storage.v2_client import _SessionPool
    pool = _SessionPool(lambda _timeout: object(), 8)
    held = [pool.acquire(45) for _ in range(12)]
    pool.resize(1)
    extra = pool.acquire(45)
    for session in [*held, extra]:
        pool.release(session, broken=False)
    assert pool.resource_snapshot()['idle'] == 1
    assert pool.resource_snapshot()['used'] == 0
    pool.close()


def test_pool_close_rejects_new_work_and_drains_idle() -> None:
    client = _client()
    client.close()
    with pytest.raises(StorageError) as excinfo:
        client.query('conversation.count', {}, deadline=1.0)
    assert excinfo.value.code == 'database_unavailable'


def test_pool_size_follows_the_daemon_connection_budget() -> None:
    from lib.storage.v2_client import _pool_size_from_budget
    assert _pool_size_from_budget(None) == 4
    # Half the advertised budget derives idle retention, never active admission.
    assert _pool_size_from_budget({'maximumConnections': 64}) == 32
    assert _pool_size_from_budget({'maximumConnections': 512}) == 256
    assert _pool_size_from_budget({'maximumConnections': 4}) == 2
    assert _pool_size_from_budget({'maximumConnections': 1}) == 1
    assert _pool_size_from_budget({'maximumConnections': 'many'}) == 4


def test_pool_retires_idle_session_past_the_freshness_cap(monkeypatch) -> None:
    """Older daemon builds close any session idle past their socket I/O
    timeout; the pool retires an idle session before any daemon build would,
    so the next RPC never discovers a peer close after writing its request."""
    monkeypatch.setattr(v2_module, '_SESSION_IDLE_CAP_S', -1.0)
    _FakeSession.responses = [_FakeResponse(0, b'{}')] * 2
    client = _client()
    client.query('conversation.count', {}, deadline=5.0)
    client.query('conversation.count', {}, deadline=5.0)
    assert _FakeSession.constructed == 2


def test_pool_retires_idle_session_that_fails_the_liveness_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        _FakeSession, 'probe_liveness', lambda self: False, raising=False)
    _FakeSession.responses = [_FakeResponse(0, b'{}')] * 2
    client = _client()
    client.query('conversation.count', {}, deadline=5.0)
    client.query('conversation.count', {}, deadline=5.0)
    assert _FakeSession.constructed == 2


def test_pool_reuses_idle_session_that_passes_the_liveness_probe(monkeypatch) -> None:
    monkeypatch.setattr(
        _FakeSession, 'probe_liveness', lambda self: True, raising=False)
    _FakeSession.responses = [_FakeResponse(0, b'{}')] * 2
    client = _client()
    client.query('conversation.count', {}, deadline=5.0)
    client.query('conversation.count', {}, deadline=5.0)
    assert _FakeSession.constructed == 1


def test_query_replays_when_a_pooled_session_was_closed_by_the_peer() -> None:
    from tofu_db.wire import StorageV2ConnectionClosedError
    _FakeSession.responses = [
        StorageV2ConnectionClosedError(
            'storage.v2 peer closed the connection at a frame boundary'),
        _FakeResponse(0, json.dumps({'count': 19}).encode()),
    ]
    client = _client()
    assert client.query('conversation.count', {}, deadline=5.0) == {'count': 19}
    assert _FakeSession.constructed == 2


def test_command_replays_a_peer_close_with_the_same_command_id() -> None:
    from tofu_db.wire import StorageV2ConnectionClosedError
    _FakeSession.responses = [
        StorageV2ConnectionClosedError(
            'storage.v2 peer closed the connection at a frame boundary'),
        _FakeResponse(0, json.dumps({'ok': True}).encode()),
    ]
    client = _client()
    result = client.command(
        'conversation.create', {'user_id': 7}, 'cid-9', deadline=5.0)
    assert result == {'ok': True}
    assert [seen['command_id'] for seen in _FakeSession.seen] == ['cid-9', 'cid-9']


def test_mid_frame_close_stays_a_fail_fast_protocol_error() -> None:
    from tofu_db.wire import StorageV2ProtocolError
    _FakeSession.responses = [
        StorageV2ProtocolError('storage.v2 peer closed mid-frame'),
        _FakeResponse(0, b'{}'),
    ]
    with pytest.raises(StorageError) as excinfo:
        _client().query('conversation.count', {}, deadline=5.0)
    assert excinfo.value.code == 'database_protocol_error'
    assert not excinfo.value.retryable
    assert _FakeSession.calls == 1


def test_readiness_budget_resizes_the_pool() -> None:
    client = _client()
    assert client._pool.size == 4
    client.set_readiness_budget({'maximumConnections': 64})
    assert client._pool.size == 32
    client.set_readiness_budget({'maximumConnections': 4})
    assert client._pool.size == 2


# ── Dedicated health lane ────────────────────────────────────────────────
def test_health_uses_a_dedicated_session_not_the_pool() -> None:
    """2026-09-23: a saturated query pool made health() fail database_busy
    while the daemon was answering, so the supervisor SIGTERMed a busy
    worker as if it were wedged. The probe must bypass the pool."""
    _FakeSession.responses = [_FakeResponse(0, json.dumps({'ready': True}).encode())]
    client = _client()
    # Saturate the query pool: with the lane in place this must not matter.
    assert client._pool.size >= 1
    result = client.health(deadline=2.0)
    assert result['ready'] is True
    # The probe consumed no pooled session: its session is the health lane's.
    assert client._health_session is not None
    assert client._pool._live == 0
    client.close()


def test_health_lane_is_reused_across_probes() -> None:
    _FakeSession.responses = [
        _FakeResponse(0, json.dumps({'ready': True}).encode()),
        _FakeResponse(0, json.dumps({'ready': True}).encode()),
    ]
    client = _client()
    assert client.health(deadline=2.0)['ready'] is True
    assert client.health(deadline=2.0)['ready'] is True
    assert _FakeSession.constructed == 1
    client.close()


def test_health_lane_broken_session_is_dropped_and_rebuilt() -> None:
    _FakeSession.responses = [
        OSError('authority unreachable'),
        _FakeResponse(0, json.dumps({'ready': True}).encode()),
    ]
    client = _client()
    assert client.health(deadline=2.0)['ready'] is False
    # The failed session is dropped, so the next probe rebuilds fresh.
    assert client._health_session is None
    assert client.health(deadline=2.0)['ready'] is True
    assert _FakeSession.constructed == 2
    client.close()


def test_health_makes_a_single_attempt_within_the_probe_deadline() -> None:
    """The supervisor's cadence is the retry loop; the lane must not multiply
    the bounded probe deadline by the read retry budget."""
    _FakeSession.responses = [
        _FakeResponse(408, b'', 'deadline_elapsed', 'slow', True),
        _FakeResponse(0, json.dumps({'ready': True}).encode()),
    ]
    client = _client()
    # deadline_elapsed is retryable on the pooled read path; the lane must
    # not retry it, so only one request is issued per probe.
    assert client.health(deadline=2.0)['ready'] is False
    assert _FakeSession.calls == 1
    client.close()


def test_close_also_closes_the_health_lane() -> None:
    _FakeSession.responses = [_FakeResponse(0, json.dumps({'ready': True}).encode())]
    client = _client()
    client.health(deadline=2.0)
    assert client._health_session is not None
    client.close()
    assert client._health_session is None

@pytest.mark.parametrize('slow_operation', ['probe', 'close'])
def test_pool_slow_socket_work_does_not_block_other_borrows_or_returns(slow_operation):
    import threading
    from lib.storage.v2_client import _SessionPool

    entered, resume = threading.Event(), threading.Event()
    returned, borrowed = threading.Event(), threading.Event()
    errors = []

    class Session:
        slow = False
        def probe_liveness(self):
            if self.slow and slow_operation == 'probe':
                entered.set()
                assert resume.wait(5)
            return True
        def close(self):
            if self.slow and slow_operation == 'close':
                entered.set()
                assert resume.wait(5)

    pool = _SessionPool(lambda timeout: Session(), 3)
    held, slow = pool.acquire(5), pool.acquire(5)
    slow.slow = True
    if slow_operation == 'probe':
        pool.release(slow, broken=False)

    def run_slow():
        try:
            if slow_operation == 'probe':
                session = pool.acquire(5)
                pool.release(session, broken=False)
            else:
                pool.release(slow, broken=True)
        except BaseException as error:
            errors.append(error)
    def return_other():
        try:
            pool.release(held, broken=False)
            returned.set()
        except BaseException as error:
            errors.append(error)
    def borrow_other():
        try:
            session = pool.acquire(5)
            borrowed.set()
            pool.release(session, broken=False)
        except BaseException as error:
            errors.append(error)

    workers = [threading.Thread(target=run_slow)]
    workers[0].start()
    try:
        assert entered.wait(2)
        workers.extend([threading.Thread(target=return_other), threading.Thread(target=borrow_other)])
        for worker in workers[1:]: worker.start()
        assert returned.wait(1), 'return blocked behind another socket operation'
        assert borrowed.wait(1), 'borrow blocked behind another socket operation'
    finally:
        resume.set()
        for worker in workers: worker.join(6)
        pool.close()
    assert not errors
    assert all(not worker.is_alive() for worker in workers)


@pytest.mark.parametrize('phase', ['probe', 'factory'])
def test_pool_close_during_reserved_session_acquisition_rejects_and_retires(phase):
    from lib.storage.v2_client import _SessionPool
    sessions = []
    close_during_acquire = False
    class Session:
        closed = False
        def probe_liveness(self):
            if close_during_acquire: pool.close()
            return True
        def close(self): self.closed = True
    def factory(timeout):
        session = Session(); sessions.append(session)
        if close_during_acquire and phase == 'factory': pool.close()
        return session
    pool = _SessionPool(factory, 1)
    if phase == 'probe':
        session = pool.acquire(1); pool.release(session, broken=False)
    close_during_acquire = True
    with pytest.raises(StorageError) as error: pool.acquire(1)
    assert error.value.code == 'database_unavailable'
    assert all(session.closed for session in sessions)
    assert pool.resource_snapshot()['used'] == 0


def test_pool_expired_probe_budget_retires_session_before_dispatch(monkeypatch):
    from lib.storage.v2_client import _SessionPool
    now = [0.0]
    monkeypatch.setattr(v2_module.time, 'monotonic', lambda: now[0])
    class Session:
        closed = False
        def probe_liveness(self): now[0] += 2; return True
        def close(self): self.closed = True
    session = Session()
    pool = _SessionPool(lambda timeout: session, 1)
    held = pool.acquire(1); pool.release(held, broken=False)
    with pytest.raises(StorageError) as error: pool.acquire(1)
    assert error.value.code == 'database_timeout'
    assert error.value.request_not_dispatched
    assert session.closed
    assert pool.resource_snapshot()['used'] == 0


def test_pool_retiring_connection_is_accounted_without_blocking_new_work():
    import threading
    from lib.storage.v2_client import _SessionPool
    entered, resume = threading.Event(), threading.Event()
    class Session:
        def close(self): entered.set(); assert resume.wait(3)
    pool = _SessionPool(lambda timeout: Session(), 1)
    session = pool.acquire(1)
    worker = threading.Thread(target=lambda: pool.release(session, broken=True)); worker.start()
    try:
        assert entered.wait(1)
        replacement = pool.acquire(.05)
        assert pool.resource_snapshot()['used'] == 2
        assert pool.resource_snapshot()['retiring'] == 1
        pool.release(replacement, broken=False)
    finally:
        resume.set(); worker.join(4)
    assert pool.resource_snapshot()['used'] == 0
    pool.close()


def test_pool_pending_liveness_probe_allows_other_long_and_short_work():
    import threading
    from lib.storage.v2_client import _SessionPool
    entered, resume = threading.Event(), threading.Event()
    errors = []
    class Session:
        slow = False
        def probe_liveness(self):
            if self.slow:
                entered.set()
                assert resume.wait(4)
                return False
            return True
        def close(self): pass
    pool = _SessionPool(lambda timeout: Session(), 4)
    idle = pool.acquire(1); idle.slow = True; pool.release(idle, broken=False)
    def long_borrow():
        try:
            session = pool.acquire(31)
            pool.release(session, broken=False)
        except BaseException as error: errors.append(error)
    worker = threading.Thread(target=long_borrow); worker.start()
    try:
        assert entered.wait(1)
        long = pool.acquire(31)
        short = pool.acquire(.5)
        pool.release(long, broken=False)
        pool.release(short, broken=False)
    finally:
        resume.set(); worker.join(5)
    assert not worker.is_alive() and not errors
    # Failed probes retire their socket before creating a replacement.
    next_long = pool.acquire(31); pool.release(next_long, broken=False)
    pool.close()


def test_health_retains_sanitized_control_error_without_retry_or_message(monkeypatch):
    client = _client()
    calls = []
    def failed_probe(operation, payload, *, deadline):
        calls.append(operation)
        raise StorageError('database_timeout', 'private-source-content', True)
    monkeypatch.setattr(client, '_health_call', failed_probe)
    result = client.health(deadline=.5)
    assert result['ready'] is False and result['probe_error'] == 'database_timeout'
    assert calls == ['system.health']
    assert 'private-source-content' not in json.dumps(result)
    client.close()


def test_health_retains_legacy_fallback_probe_error_and_keeps_not_ready(monkeypatch):
    client = _client()
    calls = []
    def failed_probe(operation, payload, *, deadline):
        calls.append(operation)
        code = 'unknown_operation' if operation == 'system.health' else 'database_unavailable'
        raise StorageError(code, 'private-source-content', True)
    monkeypatch.setattr(client, '_health_call', failed_probe)
    result = client.health(deadline=.5)
    assert result['ready'] is False and result['probe_error'] == 'database_unavailable'
    assert calls == ['system.health', 'conversation.count']
    assert 'private-source-content' not in json.dumps(result)
    client.close()


def test_health_frame_boundary_close_retires_lane_and_next_probe_reopens(monkeypatch):
    from tofu_db.wire import StorageV2ConnectionClosedError
    client = _client()
    sessions = []
    class HealthSession:
        def __init__(self): self.closed = False
        def set_timeout(self, timeout): pass
        def close(self): self.closed = True
        def request(self, *args, **kwargs):
            if len(sessions) == 1:
                raise StorageV2ConnectionClosedError('private-peer-details')
            return _FakeResponse(0, json.dumps({'ready': True}).encode())
    def open_session(timeout):
        session = HealthSession(); sessions.append(session); return session
    monkeypatch.setattr(client, '_open_session', open_session)
    result = client.health(deadline=.5)
    assert result['ready'] is False and result['probe_error'] == 'database_unavailable'
    assert len(sessions) == 1 and sessions[0].closed
    assert client._health_session is None
    assert client.health(deadline=.5)['ready'] is True
    assert len(sessions) == 2
    client.close()
    assert sessions[1].closed


def test_health_real_refused_connection_is_classified_without_a_same_probe_retry(monkeypatch):
    import socket
    from tofu_db.wire import StorageV2Session as RealSession
    client = _client()
    attempts = []
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        client._host, client._port = reserved.getsockname()
        def real_session(*args, **kwargs):
            attempts.append(1)
            return RealSession(*args, **kwargs)
        monkeypatch.setattr(v2_module, 'StorageV2Session', real_session)
        try:
            result = client.health(deadline=.1)
            assert result['ready'] is False
            assert result['probe_error'] == 'database_unavailable'
            assert attempts == [1]
            assert client._health_session is None
        finally:
            client.close()


@pytest.mark.parametrize('failure,expected', [
    (v2_module.StorageV2ConnectionClosedError('private-handshake-content'), 'database_unavailable'),
    (v2_module.StorageV2ProtocolError('private-handshake-content'), 'database_protocol_error'),
    (v2_module.StorageV2AdmissionRejected('private-handshake-content'), 'database_unavailable'),
    (TimeoutError('private-handshake-content'), 'database_unavailable'),
])
def test_health_handshake_failure_is_classified_and_next_probe_opens_fresh(monkeypatch, failure, expected):
    client = _client()
    attempts = []
    def open_session(timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            raise failure
        return _FakeSession()
    monkeypatch.setattr(client, '_open_session', open_session)
    try:
        result = client.health(deadline=.1)
        assert result['ready'] is False and result['probe_error'] == expected
        assert len(attempts) == 1 and client._health_session is None
        assert 'private-handshake-content' not in json.dumps(result)
        _FakeSession.responses = [_FakeResponse(0, json.dumps({'ready': True}).encode())]
        assert client.health(deadline=.1)['ready'] is True
        assert len(attempts) == 2
    finally:
        client.close()


@pytest.mark.parametrize('failure,expected', [
    (v2_module.StorageV2ProtocolError('private-frame-content'), 'database_protocol_error'),
    (v2_module.StorageV2AdmissionRejected('private-frame-content'), 'database_unavailable'),
])
def test_health_invalid_response_retires_lane_before_next_probe(monkeypatch, failure, expected):
    client = _client()
    sessions = []
    class Session:
        def __init__(self): self.closed = False
        def set_timeout(self, timeout): pass
        def close(self): self.closed = True
        def request(self, *args, **kwargs):
            if len(sessions) == 1: raise failure
            return _FakeResponse(0, json.dumps({'ready': True}).encode())
    def open_session(timeout):
        session = Session(); sessions.append(session); return session
    monkeypatch.setattr(client, '_open_session', open_session)
    try:
        result = client.health(deadline=.1)
        assert result['ready'] is False and result['probe_error'] == expected
        assert len(sessions) == 1 and sessions[0].closed and client._health_session is None
        assert 'private-frame-content' not in json.dumps(result)
        assert client.health(deadline=.1)['ready'] is True
        assert len(sessions) == 2
    finally:
        client.close()

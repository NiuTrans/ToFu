"""storage.v2 typed-error containment.

Native codec failures (wire.py ValueError/TypeError, JSONDecodeError,
UnicodeDecodeError, and the embedded transport's equivalents) must never
escape the client as raw builtins: they map to ``database_protocol_error``
(the v1 error class), conservatively retire the session, and feed the same
``_record_rpc`` metrics as every other failure. The v2 request path also
rejects non-finite floats with v1's error class before any session is
borrowed.

One carve-out: deterministic request-oversize rejections (the per-operation
payload bound on both v2 transports) are raised before any byte is
dispatched, so they classify as ``storage_payload_too_large`` — the v1
sidecar payload-cap code every downstream overflow fence already handles —
and the aligned session is kept.
"""

from __future__ import annotations

import json

import pytest

from lib.storage import v2_client as v2_module
from lib.storage.errors import StorageError
from lib.storage.v2_client import StorageV2Client
from lib.storage.v2_embedded_client import EmbeddedStorageV2Client

pytestmark = pytest.mark.unit

_SECRET = b's' * 48


class _FakeSession:
    """Scripted session; each construction pops the next scripted outcome."""

    outcomes: list = []
    constructed: int = 0
    closed: int = 0

    def __init__(self, *args, **kwargs):
        type(self).constructed += 1

    def set_timeout(self, _seconds):
        pass

    def request(self, operation, payload, *, command_id, deadline_unix_ms):
        outcome = type(self).outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def close(self):
        type(self).closed += 1

    def probe_liveness(self):
        return True


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch: pytest.MonkeyPatch):
    _FakeSession.outcomes = []
    _FakeSession.constructed = 0
    _FakeSession.closed = 0
    monkeypatch.setattr(v2_module, 'StorageV2Session', _FakeSession)
    yield


@pytest.fixture
def _recorded_metrics(monkeypatch: pytest.MonkeyPatch):
    records = []
    monkeypatch.setattr(
        v2_module, '_record_rpc',
        lambda kind, operation, started_at, outcome: records.append(
            (kind, operation, outcome)))
    return records


class _OkResponse:
    status = 0
    error_code = None
    error_message = None
    retryable = False

    def json(self):
        return {'count': 1}


def _client(**kwargs) -> StorageV2Client:
    kwargs.setdefault('read_attempts', 1)
    return StorageV2Client(
        '127.0.0.1', 4000, _SECRET, owner_id=7, tenant_id=3, **kwargs)


@pytest.mark.parametrize('native', [
    TypeError("unsupported storage.v2 field value: <class 'float'>"),
    json.JSONDecodeError('Expecting value', 'doc', 0),
    UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'),
])
def test_native_codec_exceptions_become_typed_protocol_errors(
        native, _recorded_metrics):
    _FakeSession.outcomes = [native]
    client = _client()
    with pytest.raises(StorageError) as excinfo:
        client.query('conversation.count', {}, deadline=1.0)
    error = excinfo.value
    assert error.code == 'database_protocol_error'
    assert type(native).__name__ in error.message
    # Metrics ran for the failure exactly like every other RPC failure.
    assert _recorded_metrics == [
        ('query', 'conversation.count', 'error')]
    client.close()


@pytest.mark.parametrize('message', [
    'storage.v2 request payload exceeds its operation bound',
    'storage.v2 request requires too many chunks',
])
def test_request_oversize_maps_to_the_payload_cap(message, _recorded_metrics):
    """wire.py's deterministic pre-dispatch rejections take the v1 payload-cap
    class so overflow fences (slim settle, cooperative abort) engage."""
    _FakeSession.outcomes = [ValueError(message)]
    client = _client()
    with pytest.raises(StorageError) as excinfo:
        client.query('conversation.count', {}, deadline=1.0)
    error = excinfo.value
    assert error.code == 'storage_payload_too_large'
    assert error.retryable is False
    assert message in error.message
    assert _recorded_metrics == [
        ('query', 'conversation.count', 'error')]
    # Close deterministically: an inline ``_client().query(...)`` drops the
    # client, and its GC-time ``__del__`` closes the retained pool session at
    # an arbitrary later point — which can land inside a later test that
    # asserts on the shared ``_FakeSession.closed`` counter.
    client.close()


def test_request_oversize_keeps_the_aligned_session():
    """Rejected before a single byte was written: the pooled session is not
    retired and serves the next RPC (no reconnect storm under sustained
    oversize rejections)."""
    _FakeSession.outcomes = [
        ValueError('storage.v2 request payload exceeds its operation bound'),
        _OkResponse(),
    ]
    client = _client(read_attempts=1)
    with pytest.raises(StorageError):
        client.query('conversation.count', {}, deadline=1.0)
    assert _FakeSession.closed == 0  # aligned session returned to the pool
    assert client.query('conversation.count', {}, deadline=1.0) == {'count': 1}
    assert _FakeSession.constructed == 1  # same session reused
    client.close()


def test_embedded_payload_bound_maps_to_the_payload_cap(_recorded_metrics):
    """The embedded transport's request-size admission is the same
    deterministic boundary failure, never a retryable availability error."""
    class _Runtime:
        closed = False
        calls = 0

        def execute_json(self, operation, payload, *, command_id,
                         deadline_unix_ms):
            type(self).calls += 1
            return {
                'status': -1,
                'error_code': 'embedded_transport_error',
                'error_message': 'embedded request payload exceeds the '
                                 'storage.v2 payload bound',
                'retryable': False,
                'payload': None,
            }

    client = EmbeddedStorageV2Client(
        _Runtime(), _SECRET, owner_id=7, tenant_id=3, read_attempts=3)
    with pytest.raises(StorageError) as excinfo:
        client.query('conversation.count', {}, deadline=1.0)
    error = excinfo.value
    assert error.code == 'storage_payload_too_large'
    assert error.retryable is False
    # Deterministic: no retry storm re-issuing an undeliverable request.
    assert _Runtime.calls == 1
    assert _recorded_metrics == [
        ('query', 'conversation.count', 'error')]
    client.close()


@pytest.mark.parametrize('native', [
    ValueError('oversize'),
    TypeError('unencodable'),
    json.JSONDecodeError('bad', 'doc', 0),
])
def test_native_exceptions_retire_the_session(native):
    _FakeSession.outcomes = [native, _OkResponse()]
    client = _client(read_attempts=1)
    with pytest.raises(StorageError):
        client.query('conversation.count', {}, deadline=1.0)
    assert _FakeSession.closed == 1  # broken session closed, not pooled
    # The next RPC builds a fresh session instead of reusing the broken one.
    assert client.query('conversation.count', {}, deadline=1.0) == {'count': 1}
    assert _FakeSession.constructed == 2
    client.close()


def test_embedded_codec_failure_is_contained_identically(
        _recorded_metrics):
    class _Runtime:
        closed = False

        def execute_json(self, operation, payload, *, command_id,
                         deadline_unix_ms):
            raise json.JSONDecodeError('Expecting value', 'doc', 0)

    client = EmbeddedStorageV2Client(
        _Runtime(), _SECRET, owner_id=7, tenant_id=3, read_attempts=1)
    with pytest.raises(StorageError) as excinfo:
        client.query('conversation.count', {}, deadline=1.0)
    assert excinfo.value.code == 'database_protocol_error'
    assert _recorded_metrics == [
        ('query', 'conversation.count', 'error')]
    client.close()


@pytest.mark.parametrize('bad_value', [float('nan'), float('inf'),
                                       float('-inf')])
def test_non_finite_floats_rejected_before_any_session(bad_value):
    client = _client()
    with pytest.raises(StorageError) as excinfo:
        client.query('conversation.list', {'score': bad_value}, deadline=1.0)
    error = excinfo.value
    assert error.code == 'database_protocol_error'
    assert 'non-finite' in error.message
    # Rejected at payload construction: no session was ever borrowed.
    assert _FakeSession.constructed == 0
    # Nested containers are scanned too.
    with pytest.raises(StorageError) as excinfo2:
        client.command('conversation.create',
                       {'tags': [{'weight': [bad_value]}]}, 'cid-1',
                       deadline=1.0)
    assert excinfo2.value.code == 'database_protocol_error'
    assert _FakeSession.constructed == 0
    client.close()


def test_finite_floats_still_flow():
    _FakeSession.outcomes = [_OkResponse()]
    client = _client()
    assert client.query(
        'conversation.list', {'score': 1.5, 'limit': 1},
        deadline=1.0) == {'count': 1}
    client.close()

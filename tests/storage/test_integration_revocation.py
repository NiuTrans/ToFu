"""Durable delivery revocation through real, disposable SQLite sidecars."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from lib.storage import StorageSupervisor
from lib.storage.errors import StorageError


pytestmark = pytest.mark.unit
_DENIED_OUTCOMES = ('failed', 'cancelled', 'stopped')


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request):
    fault = getattr(request, 'param', '')
    if fault:
        monkeypatch.setenv('TOFU_STORAGE_ENABLE_FAULT_INJECTION', '1')
        monkeypatch.setenv('TOFU_STORAGE_FAULT_ONCE', fault)
    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    try:
        supervisor.start()
        yield supervisor.client
    finally:
        supervisor.stop()


def _command(storage, operation: str, **payload):
    return storage.command(
        f'integration.workspace.{operation}', {'now': 20.0, **payload},
        f'integration-revocation:{uuid4().hex}')


def _get(storage, identity):
    return storage.query('integration.workspace.get', identity)


def _register(storage, identity, *, origin=None, now=1.0):
    return _command(
        storage, 'register', **identity, title=identity['task_id'],
        workspace_path=f"/workspace/{identity['user_id']}/{identity['task_id']}",
        managed=False, base_sha='a' * 40, now=now,
        origin_json=json.dumps(origin) if origin is not None else '')


def _ready(storage, *, task_id='task', user_id=1, now=1.0, origin=None):
    identity = {
        'user_id': user_id, 'project_root': '/project', 'task_id': task_id,
    }
    _register(storage, identity, origin=origin, now=now)
    _command(storage, 'save_checkpoint', **identity,
             checkpoint_sha='b' * 40, now=now + 0.1)
    _command(storage, 'submit', **identity, now=now + 0.2)
    return identity


def _meta(storage, identity, patch, *, now=20.0):
    return _command(storage, 'set_meta', **identity,
                    patch_json=json.dumps(patch), now=now)


def _revoke(storage, identity, outcome):
    return _meta(storage, identity, {
        'delivery': {'taskOutcome': outcome, 'state': 'preserved'},
    })


def _events(storage, identity):
    return storage.query('integration.status', {
        'user_id': identity['user_id'], 'project_root': identity['project_root'],
    })['events']


@pytest.mark.parametrize('outcome', _DENIED_OUTCOMES)
@pytest.mark.parametrize('queue_state', ('ready', 'integrating'))
def test_revocation_rejects_submit_and_stale_worker_transitions(
    storage, outcome, queue_state,
):
    identity = _ready(storage, origin={'source': 'task'})
    if queue_state == 'integrating':
        assert _command(storage, 'claim_next', now=10.0)['task_id'] == 'task'
    _revoke(storage, identity, outcome)
    revoked = _get(storage, identity)
    assert revoked['state'] == 'checkpointed'
    assert revoked['checkpoint_sha'] == 'b' * 40
    assert revoked['candidate_sha'] == ''
    assert revoked['origin'] == {
        'source': 'task',
        'delivery': {'taskOutcome': outcome, 'state': 'preserved'},
    }
    events = _events(storage, identity)

    for operation in ('submit', 'retry'):
        with pytest.raises(StorageError) as raised:
            _command(storage, operation, **identity, now=21.0)
        assert raised.value.code == 'database_conflict'
    for operation in ('requeue', 'mark_merged', 'mark_failed', 'quarantine'):
        assert _command(
            storage, operation, row_id=revoked['id'], candidate_sha='c' * 40,
            error='stale worker', now=22.0) == {'changed': False}

    assert storage.query('integration.workspace.peek_ready', {'now': 1000.0}) is None
    assert _command(storage, 'claim_next', now=1000.0) is None
    assert _get(storage, identity) == revoked
    assert _events(storage, identity) == events


@pytest.mark.parametrize('outcome', _DENIED_OUTCOMES)
@pytest.mark.parametrize('worker_failure', ('quarantine', 'mark_failed'))
def test_revoked_failed_checkpoint_cannot_retry(storage, outcome, worker_failure):
    identity = _ready(storage)
    claimed = _command(storage, 'claim_next', now=10.0)
    assert _command(storage, worker_failure, row_id=claimed['id'],
                    error='integration failure', now=11.0) == {'changed': True}
    _revoke(storage, identity, outcome)
    revoked = _get(storage, identity)
    events = _events(storage, identity)

    with pytest.raises(StorageError) as raised:
        _command(storage, 'retry', **identity)
    assert raised.value.code == 'database_conflict'
    assert _get(storage, identity) == revoked
    assert _events(storage, identity) == events
    assert _command(storage, 'claim_next', now=1000.0) is None


@pytest.mark.parametrize('outcome', _DENIED_OUTCOMES)
def test_empty_or_replacement_receipt_cannot_erase_terminal_denial(storage, outcome):
    identity = _ready(storage, origin={'source': 'task'})
    _revoke(storage, identity, outcome)
    denied = _get(storage, identity)['origin']['delivery']

    replacements = [
        {}, {'taskOutcome': ''}, {'taskOutcome': None},
        {'taskOutcome': 'completed', 'state': 'delivered'},
        *({'taskOutcome': other} for other in _DENIED_OUTCOMES if other != outcome),
    ]
    for delivery in replacements:
        result = _meta(storage, identity, {'delivery': delivery, 'attempt': 2})
        assert result['origin']['delivery'] == denied
        # In particular, empty receipt -> completed must not regain authority.
        result = _meta(storage, identity, {
            'delivery': {'taskOutcome': 'completed', 'state': 'pending'},
        })
        assert result['origin'] == {
            'source': 'task', 'attempt': 2, 'delivery': denied,
        }

    for delivery in (None, [], 'replacement'):
        with pytest.raises(StorageError) as raised:
            _meta(storage, identity, {'delivery': delivery, 'attempt': 3})
        assert raised.value.code == 'database_protocol_error'
        assert _get(storage, identity)['origin']['attempt'] == 2

    result = _meta(storage, identity, {
        'delivery': {'taskOutcome': outcome, 'reason': 'Checkpoint retained'},
    })
    assert result['origin']['delivery'] == {
        **denied, 'reason': 'Checkpoint retained',
    }
    assert _get(storage, identity)['state'] == 'checkpointed'
    with pytest.raises(StorageError) as raised:
        _command(storage, 'submit', **identity)
    assert raised.value.code == 'database_conflict'
    assert _command(storage, 'claim_next', now=1000.0) is None


@pytest.mark.parametrize('outcome', _DENIED_OUTCOMES)
def test_reregistration_cannot_replace_revoked_origin(storage, outcome):
    identity = _ready(storage, origin={'source': 'task'})
    _revoke(storage, identity, outcome)
    revoked = _get(storage, identity)
    events = _events(storage, identity)

    for origin in (None, {}, {'source': 'replacement'}, {'delivery': {}},
                   {'delivery': {'taskOutcome': 'completed'}}):
        with pytest.raises(StorageError) as raised:
            _register(storage, identity, origin=origin, now=30.0)
        assert raised.value.code == 'database_conflict'
        assert _get(storage, identity) == revoked
        assert _events(storage, identity) == events

    # Forensic checkpoint capture stays legal, but is not a new task generation.
    _command(storage, 'save_checkpoint', **identity, checkpoint_sha='d' * 40)
    assert _get(storage, identity)['origin'] == revoked['origin']
    with pytest.raises(StorageError) as raised:
        _command(storage, 'submit', **identity)
    assert raised.value.code == 'database_conflict'
    assert _command(storage, 'claim_next', now=1000.0) is None


@pytest.mark.parametrize('origin', (None, {'source': 'manual'}, {'delivery': {}}))
def test_manual_workspace_without_delivery_can_submit_retry_and_claim(storage, origin):
    identity = _ready(storage, origin=origin)
    claimed = _command(storage, 'claim_next', now=10.0)
    assert claimed['task_id'] == identity['task_id']
    assert _command(storage, 'quarantine', row_id=claimed['id'],
                    error='repair needed', now=11.0) == {'changed': True}
    _command(storage, 'retry', **identity, now=12.0)
    assert _command(storage, 'claim_next', now=13.0)['id'] == claimed['id']
    assert _command(storage, 'mark_merged', row_id=claimed['id'],
                    candidate_sha='c' * 40, now=14.0) == {'changed': True}
    assert _get(storage, identity)['state'] == 'merged'


def test_claims_and_revocation_are_scoped_to_owner_and_project(storage):
    first = _ready(storage, task_id='same', user_id=1, now=1.0)
    successor = _ready(storage, task_id='next', user_id=1, now=2.0)
    other = _ready(storage, task_id='same', user_id=2, now=3.0)
    assert _command(storage, 'claim_next', now=10.0)['user_id'] == 1
    peeked = storage.query('integration.workspace.peek_ready', {})
    assert peeked is not None and peeked['user_id'] == 2
    claimed = _command(storage, 'claim_next', now=11.0)
    assert claimed['user_id'] == 2 and claimed['task_id'] == 'same'
    assert _command(storage, 'claim_next', now=12.0) is None

    _revoke(storage, first, 'cancelled')
    assert _get(storage, other)['state'] == 'integrating'
    assert 'delivery' not in _get(storage, other)['origin']
    claimed = _command(storage, 'claim_next', now=21.0)
    assert claimed['user_id'] == 1 and claimed['task_id'] == successor['task_id']
    assert _get(storage, first)['state'] == 'checkpointed'
    assert _get(storage, other)['state'] == 'integrating'


@pytest.mark.parametrize('storage', ['integration.after_delivery_revocation'], indirect=True)
@pytest.mark.parametrize('queue_state', ('ready', 'integrating'))
def test_revocation_and_queue_removal_roll_back_together(storage, queue_state):
    identity = _ready(storage, origin={
        'delivery': {'taskOutcome': 'completed', 'state': 'pending'},
    })
    if queue_state == 'integrating':
        _command(storage, 'claim_next', now=10.0)
    before = _get(storage, identity)
    events = _events(storage, identity)
    with pytest.raises(StorageError) as raised:
        _revoke(storage, identity, 'cancelled')
    assert raised.value.code == 'database_internal'
    assert _get(storage, identity) == before
    assert _events(storage, identity) == events

    _revoke(storage, identity, 'cancelled')
    revoked = _get(storage, identity)
    assert revoked['state'] == 'checkpointed'
    assert revoked['origin']['delivery']['taskOutcome'] == 'cancelled'
    assert _command(storage, 'claim_next', now=1000.0) is None


@pytest.mark.parametrize('legacy_state', ('ready', 'integrating'))
def test_claim_rejects_legacy_queued_denial_after_restart(tmp_path, legacy_state):
    import sqlite3

    supervisor = StorageSupervisor(
        project_root=tmp_path, backend='sqlite', startup_timeout=60)
    try:
        supervisor.start()
        identity = _ready(supervisor.client)
        _revoke(supervisor.client, identity, 'cancelled')
        supervisor.stop()
        # Model a pre-fence database only while its disposable sidecar is shut
        # down. No raw connection races a live authority.
        with sqlite3.connect(tmp_path / 'data' / 'tofu.db') as database:
            database.execute(
                'UPDATE integration_workspaces SET state = ?, updated_at = 10',
                (legacy_state,))
        supervisor.start()
        assert _command(supervisor.client, 'claim_next', now=1000.0) is None
        row = _get(supervisor.client, identity)
        assert row['state'] == 'checkpointed'
        assert row['origin']['delivery']['taskOutcome'] == 'cancelled'
        assert row['checkpoint_sha'] == 'b' * 40
        assert supervisor.client.query(
            'integration.workspace.peek_ready', {'now': 1000.0}) is None
    finally:
        supervisor.stop()

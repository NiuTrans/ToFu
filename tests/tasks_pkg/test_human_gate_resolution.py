"""Public concurrency, ownership, and capacity contracts for human gates."""

from __future__ import annotations

import threading
import time

import pytest

from lib.tasks_pkg import approval, human_guidance, stdin_handler
from lib.tasks_pkg.human_gate_registry import (
    GATE_GUIDANCE,
    GATE_STDIN,
    GATE_WRITE_APPROVAL,
    OwnedHumanGateRegistry,
)


pytestmark = pytest.mark.unit
OWNER = 73
OTHER_OWNER = 74


def _wait_until(predicate, *, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError('human gate did not become pending')


def test_registry_binds_owner_and_preserves_first_resolution():
    registry = OwnedHumanGateRegistry(capacity=2)
    entry = registry.register(
        GATE_WRITE_APPROVAL, 'approval-one', owner_user_id=OWNER)

    assert entry is not None
    assert not registry.resolve(
        GATE_WRITE_APPROVAL,
        'approval-one',
        owner_user_id=OTHER_OWNER,
        response=True,
    )
    assert registry.resolve(
        GATE_WRITE_APPROVAL,
        'approval-one',
        owner_user_id=OWNER,
        response=True,
    )
    assert not registry.resolve(
        GATE_WRITE_APPROVAL,
        'approval-one',
        owner_user_id=OWNER,
        response=False,
    )
    resolution = registry.take(
        GATE_WRITE_APPROVAL, 'approval-one', entry)
    assert resolution.found is True
    assert resolution.resolved is True
    assert resolution.response is True
    assert len(registry) == 0


def test_registry_capacity_is_shared_across_gate_kinds():
    registry = OwnedHumanGateRegistry(capacity=2)

    assert registry.register(
        GATE_STDIN, 'same-id', owner_user_id=OWNER) is not None
    assert registry.register(
        GATE_GUIDANCE, 'same-id', owner_user_id=OWNER) is not None
    assert registry.register(
        GATE_WRITE_APPROVAL, 'third', owner_user_id=OWNER) is None


def test_registry_timeout_discard_cannot_erase_a_winning_response():
    registry = OwnedHumanGateRegistry(capacity=1)
    entry = registry.register(
        GATE_STDIN, 'boundary', owner_user_id=OWNER)
    assert entry is not None

    assert registry.resolve(
        GATE_STDIN,
        'boundary',
        owner_user_id=OWNER,
        response='first',
    )
    assert not registry.discard_unresolved(GATE_STDIN, 'boundary', entry)
    assert registry.take(GATE_STDIN, 'boundary', entry).response == 'first'


def test_write_approval_wait_is_owner_bound_and_first_resolution_wins():
    approval_id = 'approval-public-boundary'
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        approval.request_write_approval(
            approval_id, timeout=2, owner_user_id=OWNER)))
    waiter.start()
    _wait_until(lambda: approval.is_write_approval_pending(
        approval_id, owner_user_id=OWNER))

    assert not approval.resolve_write_approval(
        approval_id, True, owner_user_id=OTHER_OWNER)
    assert approval.resolve_write_approval(
        approval_id, True, owner_user_id=OWNER)
    assert not approval.resolve_write_approval(
        approval_id, False, owner_user_id=OWNER)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == [True]


def test_human_guidance_cancel_unblocks_only_its_owner():
    guidance_id = 'guidance-public-cancel'
    task = {'id': 'task-guidance', '_userId': OWNER, 'aborted': False}
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        human_guidance.request_human_guidance(guidance_id, task=task)))
    waiter.start()
    _wait_until(lambda: human_guidance.is_human_guidance_pending(
        guidance_id, owner_user_id=OWNER))

    assert not human_guidance.cancel_human_guidance(
        guidance_id, owner_user_id=OTHER_OWNER)
    assert human_guidance.cancel_human_guidance(
        guidance_id, owner_user_id=OWNER)
    assert not human_guidance.resolve_human_guidance(
        guidance_id, 'too late', owner_user_id=OWNER)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == [None]


def test_stdin_response_unblocks_only_its_owner():
    stdin_id = 'stdin-public-response'
    result = []
    waiter = threading.Thread(target=lambda: result.append(
        stdin_handler.request_stdin(stdin_id, owner_user_id=OWNER)))
    waiter.start()
    _wait_until(lambda: stdin_handler.is_stdin_pending(
        stdin_id, owner_user_id=OWNER))

    assert not stdin_handler.resolve_stdin(
        stdin_id, 'foreign', owner_user_id=OTHER_OWNER)
    assert stdin_handler.resolve_stdin(
        stdin_id, 'first', owner_user_id=OWNER)
    assert not stdin_handler.cancel_stdin(
        stdin_id, owner_user_id=OWNER)
    waiter.join(timeout=1)

    assert not waiter.is_alive()
    assert result == ['first']


import json

import lib.tasks_pkg.human_gate_registry as registry_module


class _FakeValueStore:

    def __init__(self):
        self.values = {}

    def set_value(self, kind, key, value, ttl):
        self.values[(kind, key)] = value

    def get_value(self, kind, key):
        return self.values.get((kind, key))

    def set_value_nx(self, kind, key, value, ttl):
        if (kind, key) in self.values:
            return False
        self.values[(kind, key)] = value
        return True

    def delete_value(self, kind, key):
        self.values.pop((kind, key), None)


@pytest.fixture
def distributed_store(monkeypatch):
    store = _FakeValueStore()
    monkeypatch.setattr(registry_module, '_distributed_mode', lambda: True)
    import lib.runtime.runtime_state_store as store_module
    monkeypatch.setattr(store_module, 'get_store', lambda: store)
    return store


def test_register_plants_request_flag(distributed_store):
    registry = OwnedHumanGateRegistry(capacity=2)
    entry = registry.register(
        GATE_WRITE_APPROVAL, 'plant', owner_user_id=OWNER)
    assert entry is not None
    assert distributed_store.get_value(
        'human-gate-req', f'{GATE_WRITE_APPROVAL}:plant') == str(OWNER)
    # take() clears the request flag so keys do not outlive the gate.
    registry.take(GATE_WRITE_APPROVAL, 'plant', entry)
    assert distributed_store.get_value(
        'human-gate-req', f'{GATE_WRITE_APPROVAL}:plant') is None


def test_cross_replica_resolve_plants_answer_first_writer_wins(
        distributed_store):
    """A resolve for a waiter on ANOTHER replica lands in the store;
    a second resolve (either replica) loses."""
    registry = OwnedHumanGateRegistry(capacity=2)
    assert registry.resolve(
        GATE_STDIN, 'remote', owner_user_id=OWNER, response='first')
    assert not registry.resolve(
        GATE_STDIN, 'remote', owner_user_id=OWNER, response='second')
    payload = json.loads(distributed_store.get_value(
        'human-gate-ans', f'{GATE_STDIN}:remote'))
    assert payload == {'owner': OWNER, 'response': 'first'}


def test_cross_replica_resolve_refuses_foreign_request_flag(
        distributed_store):
    """A pending waiter owned by someone else cannot be answered by a
    different principal, even cross-replica."""
    distributed_store.set_value(
        'human-gate-req', f'{GATE_STDIN}:foreign', str(OTHER_OWNER), 300)
    registry = OwnedHumanGateRegistry(capacity=2)
    assert not registry.resolve(
        GATE_STDIN, 'foreign', owner_user_id=OWNER, response='forged')
    assert distributed_store.get_value(
        'human-gate-ans', f'{GATE_STDIN}:foreign') is None


def test_poller_adopts_remote_answer_and_cleans_flag(distributed_store):
    registry = OwnedHumanGateRegistry(capacity=2)
    entry = registry.register(GATE_STDIN, 'adopt', owner_user_id=OWNER)
    assert entry is not None

    # Remote replica: request flag exists (planted by register) + answer.
    distributed_store.set_value(
        'human-gate-ans', f'{GATE_STDIN}:adopt',
        json.dumps({'owner': OWNER, 'response': 'remote text'}), 300)
    assert registry.poll_store_resolution(GATE_STDIN, 'adopt', entry)
    assert entry.event.is_set()
    resolution = registry.take(GATE_STDIN, 'adopt', entry)
    assert resolution.resolved is True
    assert resolution.response == 'remote text'
    assert distributed_store.get_value(
        'human-gate-ans', f'{GATE_STDIN}:adopt') is None


def test_poller_rejects_wrong_owner_answer(distributed_store):
    registry = OwnedHumanGateRegistry(capacity=2)
    entry = registry.register(GATE_GUIDANCE, 'forged', owner_user_id=OWNER)
    assert entry is not None

    distributed_store.set_value(
        'human-gate-ans', f'{GATE_GUIDANCE}:forged',
        json.dumps({'owner': OTHER_OWNER, 'response': 'x'}), 300)
    assert not registry.poll_store_resolution(GATE_GUIDANCE, 'forged', entry)
    assert not entry.event.is_set()


def test_poller_adopts_remote_cancel(distributed_store):
    registry = OwnedHumanGateRegistry(capacity=2)
    entry = registry.register(GATE_STDIN, 'cancel-remote', owner_user_id=OWNER)
    assert entry is not None

    distributed_store.set_value(
        'human-gate-ans', f'{GATE_STDIN}:cancel-remote',
        json.dumps({'owner': OWNER, 'response': None}), 300)
    assert registry.poll_store_resolution(
        GATE_STDIN, 'cancel-remote', entry)
    resolution = registry.take(GATE_STDIN, 'cancel-remote', entry)
    assert resolution.resolved is True
    assert resolution.response is None


@pytest.mark.parametrize(
    ('invoke', 'message'),
    [
        (lambda: approval.request_write_approval(
            'ownerless', timeout=0, owner_user_id=None), 'owner'),
        (lambda: human_guidance.request_human_guidance(
            'ownerless'), 'task dict'),
        (lambda: stdin_handler.request_stdin('ownerless'), 'task dict'),
    ],
)
def test_ownerless_registration_fails_closed(invoke, message):
    with pytest.raises(ValueError, match=message):
        invoke()


@pytest.mark.parametrize('answer', ['human answer', '', None])
def test_stdin_callback_retires_prompt_for_text_and_eof(monkeypatch, answer):
    from lib.tasks_pkg.handlers import code_exec
    events = []
    monkeypatch.setattr(code_exec, 'append_event', lambda task, event: events.append(event))
    monkeypatch.setattr(stdin_handler, 'request_stdin', lambda *a, **k: answer)
    row = {'toolCallId': 'stdin-call', 'status': 'searching'}
    callback = code_exec._make_stdin_callback({'id': 'task', '_userId': OWNER}, 1, row, 'read value')
    assert callback('Value?') == answer
    assert row['status'] == 'searching'
    assert 'stdinId' not in row and 'stdinPrompt' not in row
    assert [event['type'] for event in events] == ['stdin_request', 'stdin_resolved']
    assert events[0]['stdinId'] == events[1]['stdinId']


def test_stdin_wait_failure_retires_prompt_and_preserves_exception(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec
    events = []
    monkeypatch.setattr(code_exec, 'append_event', lambda task, event: events.append(event))
    def fail(*args, **kwargs):
        raise RuntimeError('injected wait failure')
    monkeypatch.setattr(stdin_handler, 'request_stdin', fail)
    row = {'toolCallId': 'stdin-call', 'status': 'searching'}
    callback = code_exec._make_stdin_callback({'id': 'task', '_userId': OWNER}, 1, row, 'read value')
    with pytest.raises(RuntimeError, match='injected wait failure'):
        callback('Value?')
    assert row['status'] == 'searching'
    assert 'stdinId' not in row and 'stdinPrompt' not in row
    assert events[-1]['type'] == 'stdin_resolved'


def test_stdin_prompt_cleanup_preserves_concurrent_terminal_status(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec
    monkeypatch.setattr(code_exec, 'append_event', lambda task, event: None)
    row = {'toolCallId': 'stdin-call', 'status': 'searching'}
    def abort(*args, **kwargs):
        row['status'] = 'aborted'
        return None
    monkeypatch.setattr(stdin_handler, 'request_stdin', abort)
    callback = code_exec._make_stdin_callback({'id': 'task', '_userId': OWNER}, 1, row, 'read value')
    assert callback('Value?') is None
    assert row['status'] == 'aborted'
    assert 'stdinId' not in row and 'stdinPrompt' not in row


@pytest.mark.parametrize('gate', ['approval', 'guidance'])
def test_cancelled_wait_releases_capacity_and_rejects_late_answer(monkeypatch, gate):
    module = approval if gate == 'approval' else human_guidance
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(module, 'human_gate_registry', registry)
    interval = '_STORE_POLL_INTERVAL' if gate == 'approval' else '_ABORT_POLL_INTERVAL'
    monkeypatch.setattr(module, interval, 0.01)
    task = {'id': 'cancelled-wait', 'aborted': False}
    result = []
    def wait():
        if gate == 'approval':
            result.append(approval.request_write_approval(
                'cancelled-wait', timeout=3600, owner_user_id=OWNER, task=task))
        else:
            result.append(human_guidance.request_human_guidance(
                'cancelled-wait', owner_user_id=OWNER, task=task))
    worker = threading.Thread(target=wait)
    worker.start()
    try:
        _wait_until(lambda: '_dispatch_heartbeat' in task)
        task['aborted'] = True
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert result == ([False] if gate == 'approval' else [None])
        assert len(registry) == 0
        kind = GATE_WRITE_APPROVAL if gate == 'approval' else GATE_GUIDANCE
        assert not registry.resolve(kind, 'cancelled-wait',
                                    owner_user_id=OWNER, response=True)
        assert registry.register(kind, 'next', owner_user_id=OWNER) is not None
    finally:
        task['aborted'] = True
        worker.join(timeout=1)


@pytest.mark.parametrize('gate', ['approval', 'guidance'])
def test_failed_abort_probe_releases_registered_waiter(monkeypatch, gate):
    module = approval if gate == 'approval' else human_guidance
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(module, 'human_gate_registry', registry)
    class BrokenTask(dict):
        def get(self, key, default=None):
            if key == 'aborted':
                raise RuntimeError('abort probe failed')
            return super().get(key, default)
    task = BrokenTask(id='broken-task')
    with pytest.raises(RuntimeError, match='abort probe failed'):
        if gate == 'approval':
            approval.request_write_approval('broken', owner_user_id=OWNER, task=task)
        else:
            human_guidance.request_human_guidance('broken', owner_user_id=OWNER, task=task)
    assert len(registry) == 0


def test_approval_response_winning_cancel_boundary_is_preserved(monkeypatch):
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(approval, 'human_gate_registry', registry)
    class RacingTask(dict):
        def get(self, key, default=None):
            if key == 'aborted':
                assert registry.resolve(GATE_WRITE_APPROVAL, 'race',
                                        owner_user_id=OWNER, response=True)
                return True
            return default
    assert approval.request_write_approval(
        'race', owner_user_id=OWNER, task=RacingTask()) is True
    assert len(registry) == 0


@pytest.mark.parametrize('gate', ['approval', 'guidance'])
def test_request_publication_can_resolve_immediately_and_failure_cleans_up(monkeypatch, gate):
    module = approval if gate == 'approval' else human_guidance
    kind = GATE_WRITE_APPROVAL if gate == 'approval' else GATE_GUIDANCE
    registry = OwnedHumanGateRegistry(capacity=1)
    monkeypatch.setattr(module, 'human_gate_registry', registry)
    request = module.request_write_approval if gate == 'approval' else module.request_human_guidance
    answer = True if gate == 'approval' else 'answer'
    def publish():
        assert not registry.resolve(kind, 'ready', owner_user_id=OTHER_OWNER, response=answer)
        assert registry.resolve(kind, 'ready', owner_user_id=OWNER, response=answer)
    assert request('ready', owner_user_id=OWNER, on_registered=publish) == answer
    assert len(registry) == 0
    def fail():
        raise RuntimeError('publication failed')
    with pytest.raises(RuntimeError, match='publication failed'):
        request('failed', owner_user_id=OWNER, on_registered=fail)
    assert len(registry) == 0
    assert registry.register(kind, 'occupied', owner_user_id=OWNER) is not None
    called = []
    assert not request('full', owner_user_id=OWNER, on_registered=lambda: called.append(True))
    assert called == []

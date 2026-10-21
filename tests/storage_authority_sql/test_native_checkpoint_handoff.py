"""Accepted semantic Turn lineage and atomic shared Rust handoff on real SQL."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import multiprocessing
import os
import threading
import time
import uuid

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage.client import StorageClient
from lib.storage_authority_sql.operations import resolve_operation_contract
from tests.storage_authority_sql.test_native_checkpoint_storage import (
    backend, opened, acquire, binding, call, rows, snapshot, HEADERS, SNAPSHOTS,
    _process_checkpoint,
)

pytestmark = pytest.mark.unit


class TransactionClient(StorageClient):
    """Exercise canonical result/event handling over the actual SQL executor."""
    def __init__(self, backend):
        self.backend = backend

    def _call(self, kind, operation, payload, **kwargs):
        receipt, handler, timeout = resolve_operation_contract(operation, kind, payload)
        if kind == 'query':
            return self.backend.query(operation, handler, time.monotonic()+20)
        return self.backend.command(operation, hashlib.sha256(orjson.dumps(payload)).hexdigest(),
            kwargs['command_id'], 'user', handler, time.monotonic()+20,
            receipt_required=receipt, transaction_timeout_s=timeout)


def accepted(backend, owner=7, target_task='target'):
    client = TransactionClient(backend)
    def run(operation, payload, *, query=False):
        payload = {**payload, 'user_id': owner}
        return client.query(operation, payload) if query else client.command(operation, payload, str(uuid.uuid4()))
    pair = run('turn.create_pair', dict(conversation_id='conversation', command_id='create',
        input_projection={'content': 'question'}, config={},
        conversation_defaults={'allowCreate': True, 'title': 'Handoff', 'settings': {}}))
    assert 'attempt' in pair, pair
    source, turn = pair['attempt']['attemptId'], pair['turn']['turnId']
    def bound(attempt, task):
        assert run('turn.attempt.claim', dict(attempt_id=attempt, dispatch_owner_id='test'))
        run('turn.attempt.bind', dict(attempt_id=attempt, task_id=task, dispatch_owner_id='test'))
        assert run('turn.attempt.start', dict(attempt_id=attempt, task_id=task))['status'] == 'running'
    bound(source, 'task')
    run('turn.event.record', dict(attempt_id=source, task_id='task', terminal=True, status='interrupted',
        slim=False, content='', thinking='checkpoint', projection={'content': '', 'thinking': 'checkpoint',
        'toolRounds': [], 'segments': []}, settlement={'outcome': 'interrupted', 'cause': 'user_abort',
        'resumeOptions': [{'operation': 'continue', 'anchor': {'type': 'replay_only', 'contentChars': 0}}]},
        error={}, event_type='aborted', event_payload={}, now=1000))
    current = run('turn.get', dict(conversation_id='conversation', turn_id=turn), query=True)
    target = run('turn.attempt.create', dict(conversation_id='conversation', turn_id=turn,
        command_id='continue', operation='continue', expected_projection_revision=current['projectionRevision'],
        config={'_resumeSource': {'taskId': 'forged'}}))
    target_attempt = target['attempt']['attemptId']
    bound(target_attempt, target_task)
    return dict(conversation_id='conversation', turn_id=turn, source_attempt_id=source,
                target_attempt_id=target_attempt, source_task_id='task', reserved_micro=0)


def seed(port):
    cap = acquire(port)
    call(port, 'load', cap)
    assert call(port, 'begin', cap, round=0)['accepted']
    assert call(port, 'complete', cap, state=snapshot())['accepted']
    assert call(port, 'release', cap)['accepted']
    return cap


def test_invoice_keeps_the_native_phase_and_settlement_shape(opened):
    port = opened()
    def invoice():
        return call(port, 'invoice', invoice=dict(owner_user_id=7, task_id='task', header_bytes=4096))['state']
    assert invoice() is None
    cap = seed(port)
    assert invoice() == {'phase': 'completed', 'wallet_settlement': None}
    cap = acquire(port, token='resumed')
    assert call(port, 'seal', cap)['accepted']
    assert invoice() == {'phase': 'terminal', 'wallet_settlement': None}


def test_real_accepted_handoff_retires_source_and_preserves_snapshot(opened, backend):
    port = opened()
    seed(port)
    lineage = accepted(backend)
    result = call(port, 'handoff', parameters=binding(task='target', token='new'), resume=lineage)
    assert result['accepted'], result
    expected = snapshot(task='target')
    assert call(port, 'load', result['capability_id'])['state'] == expected
    assert call(port, 'invoice', invoice=dict(owner_user_id=7, task_id='task', header_bytes=4096))['state'] == {
        'phase': 'terminal', 'wallet_settlement': None}
    stored = rows(backend)
    assert len(stored) == 3
    assert len([r for r in stored if r['namespace'] == SNAPSHOTS]) == 1
    assert sorted(r['version'] for r in stored if r['namespace'] == HEADERS) == [1, 5]
    assert not call(port, 'handoff', parameters=binding(task='target', token='retry'), resume=lineage)['accepted']


@pytest.mark.parametrize('difference', ['owner', 'task', 'source_attempt'])
def test_foreign_or_unaccepted_lineage_refuses_without_changing_rows(opened, backend, difference):
    port = opened()
    seed(port)
    lineage = accepted(backend, owner=8 if difference == 'owner' else 7,
                       target_task='another-task' if difference == 'task' else 'target')
    if difference == 'source_attempt':
        lineage['source_attempt_id'] = 'unaccepted-source'
    before = rows(backend)
    result = call(port, 'handoff', parameters=binding(task='target'), resume=lineage)
    assert not result['accepted'] and result['error'] == 'ineligible'
    assert rows(backend) == before


@pytest.mark.parametrize('phase', ['rollback', 'lost_ack'])
def test_handoff_faults_never_publish_partial_capabilities(opened, backend, monkeypatch, phase):
    port = opened()
    seed(port)
    lineage = accepted(backend)
    before = rows(backend)
    original = backend.command
    effects = []
    def fault(*args, **kwargs):
        if args[0] != 'native_checkpoint.handoff':
            return original(*args, **kwargs)
        if phase == 'rollback':
            operation = args[4]
            def rolled_back(session):
                effects.append(operation(session))
                raise StorageError('database_internal', 'injected handoff rollback')
            return original(*args[:4], rolled_back, *args[5:], **kwargs)
        effects.append(original(*args, **kwargs))
        raise StorageError('database_unavailable', 'injected handoff acknowledgement loss')
    monkeypatch.setattr(backend, 'command', fault)
    with pytest.raises(StorageError, match='injected handoff'):
        call(port, 'handoff', parameters=binding(task='target'), resume=lineage)
    assert effects == [{'ok': True, 'version': 1}]
    monkeypatch.setattr(backend, 'command', original)
    if phase == 'rollback':
        assert rows(backend) == before
        assert call(port, 'handoff', parameters=binding(task='target'), resume=lineage)['accepted']
    else:
        assert len(rows(backend)) == 3
        assert not call(port, 'handoff', parameters=binding(task='target'), resume=lineage)['accepted']
        assert not call(port, 'create', parameters=binding(task='target', token='attach'))['accepted']


def test_simultaneous_handoffs_commit_exactly_one_successor(opened, backend):
    ports = [opened(), opened()]
    seed(ports[0])
    lineage = accepted(backend)
    barrier = threading.Barrier(2)
    def transfer(index):
        barrier.wait(timeout=5)
        return call(ports[index], 'handoff', parameters=binding(task='target', token=f'new-{index}'), resume=lineage)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(transfer, range(2)))
    assert sum(result['accepted'] for result in results) == 1
    assert len(rows(backend)) == 3


@pytest.mark.parametrize('change', ['source_lease', 'accepted_status'])
def test_handoff_rechecks_authority_after_preparation(opened, backend, monkeypatch, change):
    port, competitor = opened(), opened()
    seed(port)
    lineage = accepted(backend)
    original = port._rows.dispatch
    changed = []
    def concurrent_change(request, deadline):
        if request['operation'] == 'handoff' and not changed:
            if change == 'source_lease':
                acquire(competitor, token='competing-source-writer')
            else:
                result = TransactionClient(backend).command('turn.event.record', dict(
                    user_id=7, attempt_id=lineage['target_attempt_id'], task_id='target', terminal=True,
                    status='interrupted', projection={'content': '', 'thinking': 'stopped'},
                    settlement={'outcome': 'interrupted', 'cause': 'user_abort'}, now=1001), str(uuid.uuid4()))
                assert result['applied']
            changed.append(rows(backend))
        return original(request, deadline)
    monkeypatch.setattr(port._rows, 'dispatch', concurrent_change)
    response = call(port, 'handoff', parameters=binding(task='target'), resume=lineage)
    assert not response['accepted']
    assert response['error'] == ('conflict' if change == 'source_lease' else 'ineligible')
    assert len(changed) == 1 and rows(backend) == changed[0]


def paid_pending():
    state = snapshot()
    state.update(tools_pending=True, messages=[{'role': 'user', 'content': 'question'},
        {'role': 'assistant', 'content': 'paid response', 'tool_calls': [
            {'id': 'file', 'type': 'function', 'function': {'name': 'write_file', 'arguments': '{}'}}]}],
        tool_recovery={'producer_task_id': 'task', 'operation': 'exact-operation'},
        shared_model_account=dict(format='tofu.native-shared-account/v1', owner_user_id=7,
            root_task_id='task', tokens=5, tool_calls=0, control_tool_calls=0, model_ordinal=1,
            tool_ordinal=0, complete=True, limits=dict(max_tokens=100, max_tool_calls=3, deadline=None),
            counter_policy=dict(encoding='cl100k_base', output_field='max_tokens', wire_model='alpha')),
        host_fee_projection=dict(format='tofu.native-fee-checkpoint/v1', owner_user_id=7, tenant_id='',
            task_id='task', logical_model='alpha', provider_id='provider', wallet_user_id='wallet', reserved_micro=10,
            rounds=[dict(round=1, cost=dict(price=dict(prompt_tokens=3), amounts=dict(outputTokens=2),
                credits=dict(input=1, output=2, cache_read=0, cache_write=0, base=3, margin=0, total=3)))],
            wallet_settlement=dict(format='tofu.native-billing/v1', task_id='task', owner_user_id=7,
                wallet_user_id='wallet', model='alpha', reserved_micro=10, roundCount=1,
                componentsMicro=dict(input=1, output=2, cache_read=0, cache_write=0,
                    baseMicro=3, marginMicro=0, totalMicro=3))))
    return state


@pytest.mark.parametrize('fault', ['none', 'rollback', 'lost_ack'])
def test_pending_handoff_requires_real_death_and_preserves_original_invoice(opened, backend, monkeypatch, fault):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    pending = paid_pending()
    process = context.Process(target=_process_checkpoint, args=(backend.config,
        os.environ['TOFU_CHECKPOINT_SQL_TEST_LIBRARY'], os.environ['TOFU_CHECKPOINT_SQL_TEST_SHA256'], child, pending))
    process.start()
    child.close()
    port = opened()
    try:
        assert parent.poll(30), 'writer did not persist paid pending frontier'
        assert parent.recv() == 'durably-pending'
        lineage = {**accepted(backend), 'reserved_micro': 25}
        before = rows(backend)
        def transfer():
            return call(port, 'handoff', parameters=binding(task='target', token='new'), resume=lineage)
        assert not transfer()['accepted']
        assert rows(backend) == before
        process.kill()
        process.join(10)
        assert process.exitcode == -9
        original = backend.command
        effects = []
        def inject(*args, **kwargs):
            if args[0] != 'native_checkpoint.handoff':
                return original(*args, **kwargs)
            if fault == 'rollback':
                def rolled_back(session):
                    effects.append(args[4](session))
                    raise StorageError('database_internal', 'pending rollback')
                return original(*args[:4], rolled_back, *args[5:], **kwargs)
            effects.append(original(*args, **kwargs))
            raise StorageError('database_unavailable', 'pending acknowledgement loss')
        if fault != 'none':
            monkeypatch.setattr(backend, 'command', inject)
            with pytest.raises(StorageError, match='pending'):
                transfer()
            assert effects == [{'ok': True, 'version': 1}]
            monkeypatch.setattr(backend, 'command', original)
            if fault == 'rollback':
                assert rows(backend) == before
        if fault == 'lost_ack':
            assert not transfer()['accepted']
            assert not call(port, 'create', parameters=binding(task='target'))['accepted']
        else:
            result = transfer()
            assert result['accepted'], result
            state = call(port, 'load', result['capability_id'])['state']
            assert state['messages'] == pending['messages']
            assert state['tool_recovery'] == pending['tool_recovery']
            assert state['shared_model_account']['root_task_id'] == 'target'
            invoice = state['host_fee_projection']['wallet_settlement']
            assert (invoice['task_id'], invoice['reserved_micro'], invoice['roundCount'],
                    invoice['componentsMicro']['totalMicro']) == ('target', 25, 0, 0)
        invoice = call(port, 'invoice', invoice=dict(owner_user_id=7, task_id='task', header_bytes=4096))['state']
        assert invoice == {'phase': 'terminal', 'wallet_settlement': pending['host_fee_projection']['wallet_settlement']}
        assert len(rows(backend)) == 3
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)
        assert not process.is_alive()
        process.close()
        parent.close()

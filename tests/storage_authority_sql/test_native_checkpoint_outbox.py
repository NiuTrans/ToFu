"""Actual native SQL capture, passive replay, prefix and transaction failures."""
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import os
from pathlib import Path
import time
import threading
import uuid

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.native_checkpoint import NativeSqlCheckpoint
from lib.storage_authority_sql.logical_outbox import decode_logical_payload
from lib.storage_authority_sql.logical_replay import BackendReplayTarget, replay_records
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend, acquire, binding, call, rows, snapshot
from tests.storage_authority_sql.test_logical_session_contract import pipeline as pipeline, target_backend as target_backend, published

pytestmark = pytest.mark.unit


@pytest.fixture
def port_factory():
    with ExitStack() as resources:
        def opened(backend, pipeline=None):
            path = os.environ.get('TOFU_CHECKPOINT_SQL_TEST_LIBRARY')
            if not path:
                pytest.skip('pinned SQL checkpoint ABI image required')
            image = Path(path)
            port = NativeSqlCheckpoint(backend=backend, tenant_id=3, owner_user_id=7,
                document_bytes=1 << 20, identity_bytes=1024, library=image,
                sha256=os.environ['TOFU_CHECKPOINT_SQL_TEST_SHA256'], binary_bytes=image.stat().st_size,
                logical_outbox=pipeline)
            resources.callback(port.close)
            return port
        yield opened


def logical_rows(backend):
    from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
    values = rows(backend)
    result = []
    for row in values:
        value = orjson.loads(row['value_json']) if isinstance(row['value_json'], (bytes, str)) else row['value_json']
        if value['format'] == SPEC['logical_replay']['passive_envelope_format']:
            assert row['namespace'] == SPEC['namespaces']['headers']
            value = {**value, 'format': SPEC['callback_format']}
        result.append((row['namespace'], row['record_key'], row['version'], value))
    return result


def target(backend, records):
    return BackendReplayTarget(backend, target_name='native', stream_id=records[0]['stream_id'])


@pytest.mark.parametrize('target_backend', ['same', 'other'], indirect=True)
def test_journal_capture_replays_exact_state_and_refuses_active_resume(backend, pipeline, target_backend, port_factory):
    port = port_factory(backend, pipeline)
    cap = acquire(port)
    assert call(port, 'load', cap)['accepted']
    assert call(port, 'begin', cap, round=0)['accepted']
    assert call(port, 'complete', cap, state=snapshot())['accepted']
    assert call(port, 'begin', cap, round=1)['accepted']
    records = published(pipeline)
    assert len(records) == 4
    assert all(record['tenant_id'] == '3' and record['owner_user_id'] == 7 for record in records)
    keep = decode_logical_payload(records[-1])
    assert len(keep['mutations']) == 1
    assert all('snapshot_json' not in step.get('sql', '') for step in keep['request']['native_checkpoint_replay']['steps'])
    replay = target(target_backend, records)
    assert replay_records(records, replay).applied_records == 4
    assert logical_rows(target_backend) == logical_rows(backend)
    from lib.storage_authority_sql.generated_checkpoint_storage_contract import SPEC
    for row in rows(target_backend):
        if row['namespace'] == SPEC['namespaces']['headers']:
            value = orjson.loads(row['value_json']) if isinstance(row['value_json'], (str, bytes)) else row['value_json']
            assert value['format'] == SPEC['logical_replay']['passive_envelope_format']
    replay.apply_and_checkpoint(records[-1], replay.checkpoint())
    copied = port_factory(target_backend)
    with pytest.raises(StorageError, match='refused'):
        call(copied, 'create', parameters=binding(token='copied'))


@pytest.mark.parametrize('damage', ['prefix', 'active', 'late_write'])
def test_replay_rejects_divergence_and_rolls_back_cursor(backend, pipeline, target_backend, port_factory, monkeypatch, damage):
    port = port_factory(backend, pipeline)
    cap = acquire(port)
    assert call(port, 'load', cap)['accepted']
    assert call(port, 'begin', cap, round=0)['accepted']
    assert call(port, 'complete', cap, state=snapshot())['accepted']
    records = published(pipeline)
    replay = target(target_backend, records)
    replay_records(records[:2], replay)
    from lib.storage_authority_sql.native_checkpoint_rows import HEADERS, FENCES
    if damage in ('prefix', 'active'):
        def alter(session):
            if damage == 'prefix':
                session.execute('UPDATE storage_records SET version=version+1 WHERE namespace=?', (HEADERS,))
            else:
                session.execute('DELETE FROM storage_records WHERE namespace=?', (FENCES,))
        target_backend.command('test.damage', '', None, 'user', alter, time.monotonic()+10, receipt_required=False)
    else:
        original = target_backend.command
        def fault(*args, **kwargs):
            def rollback(session):
                args[4](session)
                raise StorageError('database_internal', 'injected replay rollback')
            return original(*args[:4], rollback, *args[5:], **kwargs)
        monkeypatch.setattr(target_backend, 'command', fault)
    before, checkpoint = logical_rows(target_backend), replay.checkpoint()
    with pytest.raises(StorageError):
        replay_records(records[2:], replay)
    assert logical_rows(target_backend) == before
    assert replay.checkpoint() == checkpoint


@pytest.mark.parametrize('lost_ack', [False, True])
def test_capture_and_private_rows_share_rollback_and_lost_ack(backend, pipeline, port_factory, monkeypatch, lost_ack):
    port = port_factory(backend, pipeline)
    cap = acquire(port)
    assert call(port, 'load', cap)['accepted']
    assert call(port, 'begin', cap, round=0)['accepted']
    before = logical_rows(backend)
    original = backend.command
    def fault(*args, **kwargs):
        if args[0] != 'native_checkpoint.cas':
            return original(*args, **kwargs)
        if lost_ack:
            original(*args, **kwargs)
            raise StorageError('database_unavailable', 'injected lost acknowledgement')
        def rollback(session):
            args[4](session)
            raise StorageError('database_internal', 'injected capture rollback')
        return original(*args[:4], rollback, *args[5:], **kwargs)
    monkeypatch.setattr(backend, 'command', fault)
    with pytest.raises(StorageError, match='injected'):
        call(port, 'complete', cap, state=snapshot())
    monkeypatch.setattr(backend, 'command', original)
    assert (logical_rows(backend) != before) == lost_ack
    assert len(published(pipeline)) == (3 if lost_ack else 2)
    assert not call(port, 'begin', cap, round=1)['accepted']


def capture_semantics(backend, pipeline, monkeypatch):
    """Use the production capture boundary around actual fixture operations."""
    original = backend.command
    def command(*args, **kwargs):
        if args[0].startswith(('native_checkpoint.', 'logical_outbox.', 'projection.')):
            return original(*args, **kwargs)
        committed = None
        def capture(session):
            nonlocal committed
            result, committed = pipeline.execute_and_capture(session, args[4], operation=args[0],
                request_id=uuid.uuid4().hex, request_digest=args[1], command_id=args[2],
                payload={'tenant_id': '3', 'owner_user_id': 7})
            return result
        result = original(*args[:4], capture, *args[5:], **kwargs)
        pipeline.notify(committed)
        return result
    monkeypatch.setattr(backend, 'command', command)


@pytest.mark.parametrize('cost_only', [False, True])
@pytest.mark.parametrize('target_backend', ['same', 'other'], indirect=True)
def test_entire_goal_stream_replays_after_writer_death(backend, pipeline, target_backend, port_factory, monkeypatch, cost_only, record_property):
    from tests.support.native_sql_goal import GoalCase, writer, goal_call
    capture_semantics(backend, pipeline, monkeypatch)
    case = GoalCase(backend, cost_only=cost_only)
    port = port_factory(backend, pipeline)
    config = replace(backend.config, logical_shadow_mode='required', process_role='api',
                     logical_shadow_dir=pipeline.policy.sink_root)
    with writer(case, config=config) as process:
        process.kill()
        process.join(10)
        claim = case.claim(port)
        assert claim['accepted'], claim
        assert case.claim(port) == claim
        group = case.prepare_target()
        handoff = goal_call(port, 'goal_handoff', group)
        assert handoff['accepted'], handoff
        assert goal_call(port, 'goal_handoff', group) == handoff
        records = published(pipeline)
        assert sum(row['operation'] == 'native_checkpoint.goal_claim' for row in records) == 1
        assert sum(row['operation'] == 'native_checkpoint.goal_handoff' for row in records) == 1
        replay = target(target_backend, records)
        for record in records:
            try:
                assert replay_records([record], replay).applied_records == 1
            except StorageError as error:
                error.add_note(f"Fixture replay sequence={record['sequence']} operation={record['operation']}")
                raise
        assert logical_rows(target_backend) == logical_rows(backend)
        from tests.storage_authority_sql.test_native_checkpoint_handoff import TransactionClient
        restored = TransactionClient(target_backend)
        assert restored.query('billing.wallet.get', {'user_id': 'wallet-7'}) == case.query('billing.wallet.get', user_id='wallet-7')
        replay.apply_and_checkpoint(records[-1], replay.checkpoint())
        with pytest.raises(StorageError, match='refused'):
            call(port_factory(target_backend), 'create', parameters=binding(task='target-a'))
        native = [record for record in records if record['operation'].startswith('native_checkpoint.')]
        from lib.storage_authority_sql.native_checkpoint_rows import FENCES
        fences = target_backend.query('test.fence_count', lambda session: session.fetch_one(
            'SELECT COUNT(*) AS count FROM storage_records WHERE namespace=?', (FENCES,)), time.monotonic()+10)['count']
        record_property('outbox_measurement', orjson.dumps(dict(source=backend.name, target=target_backend.name,
            cost_only=cost_only, native_records=len(native), mixed_records=len(records), passive_fences=fences,
            native_wire_bytes=sum(len(orjson.dumps(row)) for row in native),
            largest_native_record_bytes=max(len(orjson.dumps(row)) for row in native))).decode())


def test_accepted_handoff_is_one_passive_replay_commit(backend, pipeline, target_backend, port_factory, monkeypatch):
    from tests.storage_authority_sql.test_native_checkpoint_handoff import accepted, seed
    capture_semantics(backend, pipeline, monkeypatch)
    port = port_factory(backend, pipeline)
    seed(port)
    lineage = accepted(backend)
    assert call(port, 'handoff', parameters=binding(task='target'), resume=lineage)['accepted']
    records = published(pipeline)
    assert sum(row['operation'] == 'native_checkpoint.handoff' for row in records) == 1
    replay = target(target_backend, records)
    replay_records(records, replay)
    assert logical_rows(target_backend) == logical_rows(backend)


def test_concurrent_actors_capture_one_contiguous_stream(backend, pipeline, target_backend, port_factory):
    port = port_factory(backend, pipeline)
    with ThreadPoolExecutor(max_workers=4) as threads:
        caps = list(threads.map(lambda index: acquire(port, task=f'actor-{index}'), range(8)))
    assert len(set(caps)) == 8
    records = published(pipeline)
    assert [row['sequence'] for row in records] == list(range(1, 9))
    assert replay_records(records, target(target_backend, records)).applied_records == 8
    assert logical_rows(target_backend) == logical_rows(backend)


@pytest.mark.parametrize('damage', ['missing', 'version', 'order', 'owner', 'codec'])
def test_private_replay_contract_fails_before_effects(backend, pipeline, target_backend, port_factory, damage):
    from lib.secret_envelope import bound_payload_cipher
    acquire(port_factory(backend, pipeline))
    records = published(pipeline)
    record = deepcopy(records[0])
    document = decode_logical_payload(record)
    if damage == 'missing':
        document['request'].pop('native_checkpoint_replay')
    elif damage == 'version':
        document['request']['native_checkpoint_replay']['format'] += '-unknown'
    elif damage == 'order':
        document['request']['native_checkpoint_replay']['steps'].append({'kind': 'mutation', 'index': 0})
    elif damage == 'owner':
        document['request']['owner_user_id'] = 8
    else:
        document['contract']['payload_codec'] = 'tofu.bound-fernet-json.v1'
        record['payload']['contract'] = document['contract']
    record['payload']['ciphertext'] = bound_payload_cipher().seal(orjson.dumps(document).decode(),
        purpose='logical-commit-payload', tenant_id=record['tenant_id'], owner_user_id=record['owner_user_id'],
        record_id=record['event_id'])
    replay = target(target_backend, records)
    with pytest.raises((StorageError, ValueError)):
        replay_records([record], replay)
    assert logical_rows(target_backend) == [] and replay.checkpoint().last_sequence == 0


def test_replace_checks_previous_snapshot_without_reading_it_for_keep(backend, pipeline, target_backend, port_factory):
    from lib.storage_authority_sql.native_checkpoint_rows import SNAPSHOTS
    from lib.storage_authority_sql.json_document import JsonBytes
    port = port_factory(backend, pipeline)
    cap = acquire(port)
    call(port, 'load', cap)
    assert call(port, 'begin', cap, round=0)['accepted']
    assert call(port, 'complete', cap, state=snapshot())['accepted']
    assert call(port, 'begin', cap, round=1)['accepted']
    state = snapshot()
    state['next_round'] = 2
    state['tool_calls'] = 2
    state['usage'].append({})
    assert call(port, 'complete', cap, state=state)['accepted']
    records = published(pipeline)
    replay = target(target_backend, records)
    replay_records(records[:4], replay)
    saved = next(row for row in logical_rows(target_backend) if row[0] == SNAPSHOTS)
    altered = deepcopy(saved[3])
    altered['value']['messages'][0]['content'] = 'divergent durable snapshot'
    target_backend.command('test.damage', '', None, 'user', lambda session: session.execute(
        'UPDATE storage_records SET value_json=? WHERE namespace=? AND record_key=?',
        (JsonBytes(orjson.dumps(altered)), SNAPSHOTS, saved[1])), time.monotonic()+10, receipt_required=False)
    before = logical_rows(target_backend)
    with pytest.raises(StorageError, match='prefix differs'):
        replay_records(records[4:], replay)
    assert logical_rows(target_backend) == before and replay.checkpoint().last_sequence == 4


@pytest.mark.parametrize('operation', ['goal_claim', 'goal_handoff'])
def test_outbox_failure_rolls_back_entire_goal_and_wallet(backend, pipeline, port_factory, monkeypatch, operation):
    from tests.support.native_sql_goal import GoalCase, writer, goal_call
    from tests.storage_authority_sql.test_native_checkpoint_goal import durable
    case, port = GoalCase(backend), port_factory(backend, pipeline)
    with writer(case) as process:
        process.kill()
        process.join(10)
        group = None
        if operation == 'goal_handoff':
            assert case.claim(port)['accepted']
            group = case.prepare_target()
        before = durable(case)
        previous = len(published(pipeline))
        original = pipeline.capture
        def fail(*args, **kwargs):
            original(*args, **kwargs)
            raise StorageError('database_internal', 'injected outbox capture failure')
        monkeypatch.setattr(pipeline, 'capture', fail)
        with pytest.raises(StorageError, match='injected outbox'):
            case.claim(port) if group is None else goal_call(port, operation, group)
        assert durable(case) == before
        assert len(published(pipeline)) == previous


def test_goal_replay_refuses_changed_wallet_and_preserves_whole_actor_prefix(backend, pipeline, target_backend, port_factory, monkeypatch):
    from tests.support.native_sql_goal import GoalCase, writer
    capture_semantics(backend, pipeline, monkeypatch)
    case, port = GoalCase(backend), port_factory(backend, pipeline)
    config = replace(backend.config, logical_shadow_mode='required', process_role='api',
                     logical_shadow_dir=pipeline.policy.sink_root)
    with writer(case, config=config) as process:
        process.kill()
        process.join(10)
        assert case.claim(port)['accepted']
        records = published(pipeline)
        assert records[-1]['operation'] == 'native_checkpoint.goal_claim'
        replay = target(target_backend, records)
        replay_records(records[:-1], replay)
        target_backend.command('test.wallet_change', '', None, 'user', lambda session: session.execute(
            'UPDATE billing_wallets SET balance_micro=balance_micro+1 WHERE user_id=?', ('wallet-7',)),
            time.monotonic()+10, receipt_required=False)
        before, cursor = logical_rows(target_backend), replay.checkpoint()
        with pytest.raises(StorageError, match='prefix differs'):
            replay_records(records[-1:], replay)
        assert logical_rows(target_backend) == before and replay.checkpoint() == cursor


def test_private_replay_refuses_forwarding(backend, pipeline, target_backend, port_factory, monkeypatch):
    acquire(port_factory(backend, pipeline))
    records = published(pipeline)
    monkeypatch.setattr(target_backend, 'config', replace(target_backend.config, logical_shadow_mode='required'))
    replay = target(target_backend, records)
    with pytest.raises(StorageError, match='cannot forward'):
        replay_records(records, replay)
    assert logical_rows(target_backend) == [] and replay.checkpoint().last_sequence == 0


def test_close_joins_outbox_capture_and_durable_commit(backend, pipeline, port_factory, monkeypatch):
    port = port_factory(backend, pipeline)
    entered, release = threading.Event(), threading.Event()
    original = pipeline.capture
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(pipeline, 'capture', blocked)
    with ThreadPoolExecutor(max_workers=2) as threads:
        active = threads.submit(acquire, port)
        assert entered.wait(5)
        closing = threads.submit(port.close)
        try:
            with port._condition:
                assert port._condition.wait_for(lambda: port._state == 'closing', timeout=5)
            assert not closing.done()
        finally:
            release.set()
        assert active.result(timeout=10) > 0
        closing.result(timeout=10)
    assert len(logical_rows(backend)) == 1 and len(published(pipeline)) == 1


def test_required_capture_refuses_a_disabled_pipeline(backend, pipeline, port_factory, monkeypatch):
    from lib.native_binary import NativeConfigurationError
    monkeypatch.setattr(backend, 'config', replace(backend.config, logical_shadow_mode='required'))
    monkeypatch.setattr(pipeline, 'policy', replace(pipeline.policy, mode='off', capture_enabled=False))
    with pytest.raises(NativeConfigurationError, match='configuration'):
        port_factory(backend, pipeline)
    assert logical_rows(backend) == []

"""Joined callback lifetime and protected preparations for whole SQL Goal moves."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import threading
import time

import orjson
import pytest

from lib.storage.errors import StorageError
from lib.storage_authority_sql.native_checkpoint import NativeSqlCheckpointError
from lib.storage_authority_sql.native_checkpoint_goal import GoalTransaction
from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend, opened as opened, rows
from tests.storage_authority_sql.test_native_checkpoint_goal import durable
from tests.support.native_sql_goal import GoalCase, goal_call, writer, EVIDENCE

pytestmark = pytest.mark.unit


def test_prepared_actor_witness_is_bound_to_each_protected_source_header(opened, backend):
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        assert case.claim(port)['accepted']
        group = case.prepare_target()
        auth = group['authorization']
        for binding in auth['actors']:
            source, task = binding['source_task_id'], binding['target_task_id']
            birth = case.query('record.get', namespace='task_results', key=task)['value']['child_invocation']
            event = dict(type='execution_effect_prepared', key=f'root:agent:{source}:0:0', kind='agent',
                input_hash='a'*64, invocation_id=task, invocation_completion=dict(format='tofu.flow.invocation-completion/v1',
                    nonce='12345678901234567890123456789012', account_root_task_id='target-root', identity=birth))
            case.append('target-root', auth['target_cursor'], event)
            auth['target_cursor'] += 1
        assert goal_call(port, 'goal_handoff', group)['accepted']
        events = case.query('orchestration.event.page', run_id='goal_target-root', tenant_id='3', cursor=auth['target_cursor'])['events']
        consumed = next(event for event in events if event['type'] == 'execution_recovery_consumed')
        stored = []
        for row in rows(backend):
            value = row['value_json']
            stored.append(orjson.loads(value) if isinstance(value, (str, bytes)) else value)
        for actor in consumed['actors']:
            header = next(item['value'] for item in stored if item['task_id'] == actor['source_task_id'])
            assert header['goal_actor_handoff']['witness_sha256'] == hashlib.sha256(orjson.dumps(actor, option=orjson.OPT_SORT_KEYS)).hexdigest()
            assert actor['preparation']['invocation_id'] == actor['target_task_id']
            assert header['goal_actor_handoff']['target_task_id'] == actor['target_task_id']
            assert header['goal_actor_claim']['request_sha256'] == hashlib.sha256(
                orjson.dumps(case.request, option=orjson.OPT_SORT_KEYS)).hexdigest()


@pytest.mark.parametrize('mode', ['close', 'deadline'])
def test_goal_transaction_joins_entered_sql_before_code_or_context_can_retire(opened, backend, monkeypatch, mode):
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        assert case.claim(port)['accepted']
        group = case.prepare_target()
        before = durable(case)
        entered, release = threading.Event(), threading.Event()
        original = GoalTransaction.dispatch
        observed = []
        def held(self, request):
            result = original(self, request)
            if request['operation'] == 'goal.put' and not entered.is_set():
                # Both callbacks execute on SQLite's actual writer thread;
                # neither may enter another writer job or wait for itself.
                with pytest.raises(NativeSqlCheckpointError, match='reentrant'):
                    goal_call(port, 'goal_handoff', group)
                with pytest.raises(NativeSqlCheckpointError, match='callback'):
                    port.close()
                observed.append(threading.get_ident())
                entered.set()
                assert release.wait(10)
            return result
        monkeypatch.setattr(GoalTransaction, 'dispatch', held)
        def invoke():
            return port.control('goal_handoff', {'format': 'tofu.native-checkpoint-control/v1', 'goal_handoff': group},
                                deadline_at=time.monotonic()+(0.2 if mode == 'deadline' else 20), response_bytes=EVIDENCE+4096)
        with ThreadPoolExecutor(max_workers=2) as executor:
            pending = executor.submit(invoke)
            try:
                assert entered.wait(5)
                closing = executor.submit(port.close) if mode == 'close' else None
                with pytest.raises(TimeoutError):
                    (closing or pending).result(timeout=0.25)
            finally:
                release.set()
            if mode == 'close':
                assert pending.result(timeout=10)['accepted']
                closing.result(timeout=10)
            else:
                with pytest.raises((StorageError, NativeSqlCheckpointError)):
                    pending.result(timeout=10)
        assert len(observed) == 1
        if mode == 'deadline':
            assert durable(case) == before
            assert goal_call(port, 'goal_handoff', group)['accepted']
        else:
            assert len(rows(backend)) == 6
            with pytest.raises(NativeSqlCheckpointError, match='closed'):
                goal_call(port, 'goal_handoff', group)


def test_claim_scope_and_incomplete_or_changed_authority_never_retire_sources(opened, backend):
    from copy import deepcopy
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        before = durable(case)
        for damage in ('membership', 'registration', 'version', 'cursor', 'invoice', 'mode', 'schema'):
            request = deepcopy(case.request)
            if damage == 'membership':
                request['actors'].pop(0)
            elif damage == 'registration':
                request['actors'][0]['registration_version'] += 1
            elif damage == 'version':
                request['actors'][0]['checkpoint_version'] += 1
            elif damage == 'cursor':
                request['source_cursor'] += 1
            elif damage == 'invoice':
                request['invoice']['reserved_micro'] -= 1
            elif damage == 'mode':
                request['invoice']['format'] = 'tofu.native-aggregate-cost/v1'
                request['invoice'].pop('wallet_user_id')
                request['invoice'].pop('reserved_micro')
            else:
                request['extra'] = True
            assert not case.claim(port, request)['accepted'], damage
            assert durable(case) == before
        for scope in ({'owner': 8}, {'tenant': 4}):
            assert not case.claim(opened(**scope))['accepted']
            assert durable(case) == before
        assert case.claim(port)['accepted']


def test_ordinary_appended_claim_cannot_forge_protected_actor_authority(opened, backend):
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        fake = dict(type='execution_recovery_claim', successor_run_id='goal_target-root', native_actor_claim=dict(
            format='tofu.native-goal-actor-claim/v1', request_sha256=hashlib.sha256(orjson.dumps(case.request, option=orjson.OPT_SORT_KEYS)).hexdigest(),
            target_attempt_id=case.request['target_attempt_id'], source_cursor=case.request['source_cursor'],
            account_version=case.source['cut']['version'], invoice=case.request['invoice'],
            actors=[dict(task_id=task, source_version=3, version=4) for task in ('pending-a', 'pending-b')]))
        case.append('source-root', case.request['source_cursor'], fake)
        before = durable(case)
        assert not case.claim(port)['accepted']
        assert durable(case) == before

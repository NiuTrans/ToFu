"""Whole Goal Rust policy and SQL atomicity on real SQLite/PostgreSQL backends."""
from copy import deepcopy

import pytest

from tests.storage_authority_sql.test_native_checkpoint_storage import backend as backend, opened as opened, rows, call
from tests.support.native_sql_goal import GoalCase, goal_call, writer

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('cost_only', [False, True])
def test_whole_goal_claim_and_consumption_preserve_history_and_original_debt(opened, backend, cost_only):
    case, port = GoalCase(backend, cost_only=cost_only), opened()
    with writer(case) as process:
        before = rows(backend)
        assert not case.claim(port)['accepted']
        assert rows(backend) == before
        process.kill()
        process.join(10)
        claim = case.claim(port)
        assert claim['accepted'], claim
        assert case.claim(port) == claim
        wallet = case.query('billing.wallet.get', user_id='wallet-7')
        assert wallet['balance_micro'] == (200000 if cost_only else 177221)
        group = case.prepare_target()
        wallet = case.query('billing.wallet.get', user_id='wallet-7')
        response = goal_call(port, 'goal_handoff', group)
        assert response['accepted'], response
        assert goal_call(port, 'goal_handoff', deepcopy(group)) == response
        leases = response['state']['leases']
        assert len(leases) == 2 and len({row['capability_id'] for row in leases}) == 2
        for row, source in zip(leases, ('pending-a', 'pending-b')):
            loaded = call(port, 'load', row['capability_id'])
            assert loaded['accepted']
            state = loaded['state']
            assert state['task_id'] == row['task_id']
            assert state['shared_model_account']['root_task_id'] == 'target-root'
            for key in ('messages', 'usage', 'tool_calls', 'progress', 'round_limit', 'tool_limit'):
                assert state[key] == case.source['states'][source][key]
            expected_fees = {**case.source['states'][source]['host_fee_projection'], 'task_id': row['task_id']}
            assert state['host_fee_projection'] == expected_fees
        assert case.query('billing.wallet.get', user_id='wallet-7') == wallet
        assert len(rows(backend)) == 6
        assert case.claim(port) == claim
        assert not goal_call(opened(), 'goal_handoff', group)['accepted']


def durable(case):
    return dict(rows=rows(case.backend), wallet=case.query('billing.wallet.get', user_id='wallet-7'),
                source=case.cursor('source-root'), target=case.cursor('target-root'))


@pytest.mark.parametrize('operation', ['goal_claim', 'goal_handoff'])
@pytest.mark.parametrize('phase', ['after_writes', 'lost_ack'])
def test_whole_goal_delivery_faults_never_publish_a_partial_group(opened, backend, monkeypatch, operation, phase):
    from lib.storage.errors import StorageError
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        group = None
        if operation == 'goal_handoff':
            assert case.claim(port)['accepted']
            group = case.prepare_target()
        before = durable(case)
        original = backend.command
        completed = []
        def fault(*args, **kwargs):
            if args[0] != 'native_checkpoint.' + operation:
                return original(*args, **kwargs)
            if phase == 'after_writes':
                def rollback(session):
                    completed.append(args[4](session))
                    raise StorageError('database_internal', 'injected whole Goal rollback')
                return original(*args[:4], rollback, *args[5:], **kwargs)
            completed.append(original(*args, **kwargs))
            raise StorageError('database_unavailable', 'injected whole Goal lost acknowledgement')
        monkeypatch.setattr(backend, 'command', fault)
        def perform():
            return case.claim(port) if operation == 'goal_claim' else goal_call(port, operation, group)
        with pytest.raises(StorageError, match='injected whole Goal'):
            perform()
        assert len(completed) == 1 and completed[0]['ok']
        monkeypatch.setattr(backend, 'command', original)
        if phase == 'after_writes':
            assert durable(case) == before
            assert perform()['accepted']
        else:
            assert durable(case) != before
            if operation == 'goal_claim':
                assert perform()['accepted']
            else:
                assert not perform()['accepted']
                for task in ('target-a', 'target-b'):
                    from tests.storage_authority_sql.test_native_checkpoint_storage import binding
                    assert not call(port, 'create', parameters=binding(task=task, token='attach'))['accepted']
                assert len(rows(backend)) == 6


@pytest.mark.parametrize('damage', ['account', 'registration', 'membership', 'cursor', 'reserve', 'schema', 'bound'])
def test_whole_goal_refusal_preserves_every_actor_wallet_and_event(opened, backend, damage):
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        assert case.claim(port)['accepted']
        group = case.prepare_target()
        changed = deepcopy(group)
        auth = changed['authorization']
        if damage == 'account':
            auth['account_frontiers'][1]['snapshot']['tokens'] = 999999
        elif damage == 'registration':
            auth['actors'][1]['target_registration_version'] += 1
        elif damage == 'membership':
            auth['actors'].pop()
        elif damage == 'cursor':
            auth['target_cursor'] += 1
        elif damage == 'reserve':
            auth['target_reserved_micro'] -= 1
        elif damage == 'schema':
            auth['extra'] = True
        else:
            changed['evidence_bytes'] = 1024
        before = durable(case)
        assert not goal_call(port, 'goal_handoff', changed)['accepted']
        assert durable(case) == before
        assert goal_call(port, 'goal_handoff', group)['accepted']


def test_policy_failure_after_wallet_write_rolls_back_retirement_and_all_money(opened, backend, monkeypatch):
    from lib.storage_authority_sql.native_checkpoint_goal import GoalTransaction
    case, port = GoalCase(backend), opened()
    with writer(case) as process:
        process.kill()
        process.join(10)
        before = durable(case)
        original = GoalTransaction.settle
        observed = []
        def refused(self, **intent):
            value = original(self, **intent)
            assert value['applied']
            observed.append(value)
            return {**value, 'applied': False}
        monkeypatch.setattr(GoalTransaction, 'settle', refused)
        assert not case.claim(port)['accepted']
        assert len(observed) == 1
        assert durable(case) == before
        monkeypatch.setattr(GoalTransaction, 'settle', original)
        assert case.claim(port)['accepted']


def test_concurrent_whole_consumers_publish_one_group(opened, backend):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    case, ports = GoalCase(backend), [opened(), opened()]
    with writer(case) as process:
        process.kill()
        process.join(10)
        assert case.claim(ports[0])['accepted']
        group = case.prepare_target()
        barrier = threading.Barrier(2)
        def perform(index):
            barrier.wait(timeout=5)
            return goal_call(ports[index], 'goal_handoff', group)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(perform, range(2)))
        assert sum(result['accepted'] for result in results) == 1
        assert len(rows(backend)) == 6
        assert case.cursor('target-root') == group['authorization']['target_cursor'] + 3

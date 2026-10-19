"""Terminal callback/poll interleavings preserve one receipt and one successor."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from lib.storage.errors import StorageError
from tests.evolution.test_evolution_runs import OWNER, review, runtime as runtime

pytestmark = pytest.mark.unit


def terminal(task, content='Observed complete delivery.'):
    task.update(status='done', finishReason='stop', content=content,
                finished_at=task['created_at'] + 1,
                usage={'input_tokens': 10, 'output_tokens': 5})
    # Commit through the real terminal result boundary while withholding the
    # callback. Poll and callback consumers then race over committed evidence.
    from lib.tasks_pkg.manager import _persist, _terminal
    from lib.agent_core.execution_session import ExecutionPhase
    task['_executionSession'].settle(ExecutionPhase.COMPLETED, cause='controlled_terminal_race')
    with patch.object(_terminal, 'notify_terminal_listeners', side_effect=RuntimeError('withheld callback')):
        assert _persist.persist_task_result(task, _defer_heavy_release=True) is False
    return task


@pytest.mark.parametrize('callback', [False, True])
def test_winning_consumer_is_returned_without_duplicate_usage_or_review(runtime, monkeypatch, callback):
    from lib.evolution import records
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Deliver and independently review once.'}, user_id=OWNER)
    executor = terminal(tasks[0])
    original = records.put_record
    rival = []

    def interleave(namespace, key, value, **kwargs):
        if (not rival and namespace == records.NAMESPACE_RUNS
                and value['status'] == 'preparing'
                and value.get('stage_launch', {}).get('stage') == 'reviewing'):
            rival.append(None)
            rival[0] = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
        return original(namespace, key, value, **kwargs)

    monkeypatch.setattr(records, 'put_record', interleave)
    current = runs.advance_run(run['run_id'], user_id=OWNER,
                               terminal_task=executor if callback else None)
    assert current == rival[0] == runs.get_run(run['run_id'], user_id=OWNER)
    assert current['status'] == 'reviewing'
    assert len(tasks) == 2
    assert [row['task_id'] for row in current['receipts']] == [executor['id']]
    assert current['actuals']['tokens'] == 15
    assert runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor) == current
    assert len(tasks) == 2


def test_unrelated_version_change_does_not_acknowledge_terminal_delivery(runtime, monkeypatch):
    from lib.evolution import records
    from lib.agent_core.terminal_delivery import deliver_terminal
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep unconsumed completion retryable.'}, user_id=OWNER)
    executor = terminal(tasks[0])
    original = records.put_record
    changed = []

    def interleave(namespace, key, value, **kwargs):
        if not changed and namespace == records.NAMESPACE_RUNS and value['status'] == 'preparing':
            changed.append(True)
            current, version = records.get_versioned_record(namespace, key)
            current = deepcopy(current)
            current['error'] = 'An unrelated owner update.'
            original(namespace, key, current, kind='run', expected_version=version)
        return original(namespace, key, value, **kwargs)

    monkeypatch.setattr(records, 'put_record', interleave)
    with pytest.raises(StorageError) as failed:
        deliver_terminal(executor)
    assert failed.value.code == 'database_conflict'
    assert not executor['_terminalDelivery']._acknowledged
    assert not runs.get_run(run['run_id'], user_id=OWNER)['receipts']
    assert len(tasks) == 1
    monkeypatch.setattr(records, 'put_record', original)
    deliver_terminal(executor)
    current = runs.get_run(run['run_id'], user_id=OWNER)
    assert executor['_terminalDelivery']._acknowledged
    assert current['status'] == 'reviewing' and current['actuals']['tokens'] == 15
    assert len(current['receipts']) == 1 and len(tasks) == 2


@pytest.mark.parametrize('code', ['database_conflict', 'database_unavailable'])
def test_storage_failure_without_proven_successor_remains_visible(runtime, monkeypatch, code):
    from lib.evolution import records
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Do not hide unacknowledged writes.'}, user_id=OWNER)
    executor = terminal(tasks[0])
    original = records.put_record

    def refuse(namespace, key, value, **kwargs):
        if namespace == records.NAMESPACE_RUNS:
            raise StorageError(code, 'Injected write refusal.')
        return original(namespace, key, value, **kwargs)

    monkeypatch.setattr(records, 'put_record', refuse)
    with pytest.raises(StorageError) as failed:
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
    assert failed.value.code == code
    assert not runs.get_run(run['run_id'], user_id=OWNER)['receipts']
    assert len(tasks) == 1


@pytest.mark.parametrize('callback', [False, True])
def test_owner_cancellation_wins_without_dispatch_or_lost_consumption(runtime, monkeypatch, callback):
    from lib.evolution import records
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve cancellation during completion.'}, user_id=OWNER)
    executor = terminal(tasks[0])
    original = records.put_record
    cancelled = []

    def interleave(namespace, key, value, **kwargs):
        if not cancelled and namespace == records.NAMESPACE_RUNS and value['status'] == 'preparing':
            cancelled.append(True)
            runs.cancel_run(run['run_id'], user_id=OWNER)
        return original(namespace, key, value, **kwargs)

    monkeypatch.setattr(records, 'put_record', interleave)
    if callback:
        with pytest.raises(StorageError) as failed:
            runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
        assert failed.value.code == 'database_conflict'
    else:
        assert runs.advance_run(run['run_id'], user_id=OWNER)['status'] == 'cancelled'
    assert len(tasks) == 1
    monkeypatch.setattr(records, 'put_record', original)
    current = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
    assert current['status'] == 'cancelled' and current['actuals']['tokens'] == 15
    assert len(current['receipts']) == 1 and len(tasks) == 1


@pytest.mark.parametrize('boundary', ['before_episode_read', 'before_episode_commit'])
@pytest.mark.parametrize('callback', [False, True])
def test_identical_terminal_settlement_confirms_winner_without_rewriting_episode(
        runtime, monkeypatch, boundary, callback):
    from lib.evolution import episodes, records
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Settle the independently reviewed result once.'}, user_id=OWNER)
    runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=terminal(tasks[0]))
    reviewer = terminal(tasks[1], review())
    rival = []
    target, name = ((runs, 'settle_episode') if boundary == 'before_episode_read'
                    else (episodes, 'put_record'))
    original = getattr(target, name)

    def interleave(*args, **kwargs):
        if not rival:
            rival.append(None)
            rival[0] = runs.advance_run(run['run_id'], user_id=OWNER)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, interleave)
    completed = runs.advance_run(run['run_id'], user_id=OWNER,
                                terminal_task=reviewer if callback else None)
    assert completed == rival[0] == runs.get_run(run['run_id'], user_id=OWNER)
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 30
    assert len(tasks) == len(completed['receipts']) == 2
    episode = episodes.get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'success' and episode['actuals']['tokens'] == 30
    assert store.versions[(records.NAMESPACE_EPISODES, f'{OWNER}:{run["episode_id"]}')] == 2
    writes = store.writes
    assert runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=reviewer) == completed
    assert store.writes == writes


@pytest.mark.parametrize('changed', [None, 'actuals', 'verdict', 'completeness'])
def test_interrupted_settlement_recovery_requires_exact_committed_evidence(runtime, monkeypatch, changed):
    from lib.evolution import episodes, records
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Recover only the same verified settlement.'}, user_id=OWNER)
    runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=terminal(tasks[0]))
    reviewer = terminal(tasks[1], review())
    original = runs.settle_episode

    def commit_without_ack(*args, **kwargs):
        intent = deepcopy(kwargs)
        if changed == 'actuals':
            intent['actuals']['tokens'] += 1
        elif changed == 'verdict':
            intent['verdict']['criterion_results'][0]['evidence_ref'] = 'A different verified source.'
        elif changed == 'completeness':
            intent['actuals_complete'] = False
        original(*args, **intent)
        raise RuntimeError('settlement acknowledgement unavailable')

    monkeypatch.setattr(runs, 'settle_episode', commit_without_ack)
    with pytest.raises(RuntimeError, match='acknowledgement unavailable'):
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=reviewer)
    waiting = runs.get_run(run['run_id'], user_id=OWNER)
    assert waiting['status'] == 'settling'
    settled = episodes.get_episode(run['episode_id'], user_id=OWNER)
    monkeypatch.setattr(runs, 'settle_episode', original)
    if changed:
        writes = store.writes
        with pytest.raises(runs.EvolutionStateError, match='conflicts'):
            runs.advance_run(run['run_id'], user_id=OWNER)
        assert store.writes == writes
        assert runs.get_run(run['run_id'], user_id=OWNER) == waiting
    else:
        completed = runs.advance_run(run['run_id'], user_id=OWNER)
        assert completed['status'] == 'completed'
        assert completed['actuals'] == settled['actuals']
    assert episodes.get_episode(run['episode_id'], user_id=OWNER) == settled
    assert store.versions[(records.NAMESPACE_EPISODES, f'{OWNER}:{run["episode_id"]}')] == 2
    assert len(tasks) == 2


def test_cancelled_episode_settlement_race_keeps_consumption_and_cancellation(runtime, monkeypatch):
    from lib.evolution import episodes, records
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Retain cancellation and final consumption.'}, user_id=OWNER)
    runs.cancel_run(run['run_id'], user_id=OWNER)
    executor = terminal(tasks[0])
    original = runs.settle_episode
    rival = []

    def interleave(*args, **kwargs):
        if not rival:
            rival.append(None)
            rival[0] = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
        return original(*args, **kwargs)

    monkeypatch.setattr(runs, 'settle_episode', interleave)
    current = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
    assert current == rival[0]
    assert current['status'] == 'cancelled' and current['actuals']['tokens'] == 15
    settled = episodes.get_episode(run['episode_id'], user_id=OWNER)
    assert settled['status'] == 'aborted' and settled['actuals']['tokens'] == 15
    assert store.versions[(records.NAMESPACE_EPISODES, f'{OWNER}:{run["episode_id"]}')] == 2
    assert len(tasks) == len(current['receipts']) == 1

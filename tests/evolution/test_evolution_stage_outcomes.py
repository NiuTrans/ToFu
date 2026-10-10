"""Terminal archive crash boundaries, ownership, evidence, accounting, and paging."""
from __future__ import annotations

import hashlib

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, review

pytestmark = pytest.mark.unit


def terminal(task, content='Observed result', **fields):
    task.update(status='done', finishReason='stop', content=content,
                usage={'input_tokens': 10, 'output_tokens': 5},
                finished_at=task['created_at'] + 1, **fields)
    return task


def frozen(runtime, monkeypatch, content='Observed result'):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Test the original hypothesis.'}, user_id=OWNER)
    original = runs._prepare_stage
    def crash(*args, **kwargs):
        raise RuntimeError('process loss before run receipt commit')
    monkeypatch.setattr(runs, '_prepare_stage', crash)
    task = terminal(tasks[0], content)
    with pytest.raises(RuntimeError, match='process loss'):
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task)
    monkeypatch.setattr(runs, '_prepare_stage', original)
    return run, task


def test_completed_executor_survives_lost_process_without_executor_replay(runtime, monkeypatch):
    from lib.evolution import stage_outcomes as outcomes
    runs, _, tasks = runtime
    run, executor = frozen(runtime, monkeypatch)
    manifest = outcomes.read_metadata(run['run_id'], executor['id'], user_id=OWNER)
    assert manifest['archive_state'] == 'sealed'
    assert runs.get_run(run['run_id'], user_id=OWNER)['receipts'] == []
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    recovered = runs.advance_run(run['run_id'], user_id=OWNER)
    assert recovered['status'] == 'reviewing' and len(tasks) == 2
    assert recovered['result'] == 'Observed result'
    assert recovered['receipts'][0]['restored_from_outcome'] is True
    assert recovered['actuals']['tokens'] == 15
    assert tasks[1]['messages'][0]['content'] == manifest['review_prompt']
    runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
    assert len(tasks) == 2
    completed = runs.advance_run(run['run_id'], user_id=OWNER,
                                 terminal_task=terminal(tasks[1], review()))
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 30


def test_restored_review_uses_frozen_tool_evidence_even_if_runtime_bytes_change(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Measure the mechanism.'}, user_id=OWNER)
    executor = terminal(tasks[0])
    executor['messages'].append({'role': 'tool', 'tool_call_id': 'measured-1',
                                'content': 'Controlled baseline: 81; proposed mechanism: 42.'})
    from lib.evolution.stage_outcomes import capture
    original = capture(run, executor, user_id=OWNER,
                       review_prompt_factory=lambda content: runs._review_prompt({**run, 'result': content}, executor))
    executor['messages'][-1]['content'] = 'Uncommitted fabricated measurement'
    executor['content'] = 'Uncommitted fabricated result'
    recovered = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=executor)
    assert recovered['result'] == 'Observed result'
    assert tasks[1]['messages'][0]['content'] == original['review_prompt']
    assert 'Controlled baseline: 81' in original['review_prompt']
    assert 'fabricated' not in tasks[1]['messages'][0]['content']


def test_complete_chunks_recover_after_loss_before_archive_seal(runtime, monkeypatch):
    from lib.evolution import records, stage_outcomes as outcomes
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve a completed experiment.'}, user_id=OWNER)
    original = records.put_record
    def crash(namespace, key, value, **kwargs):
        if namespace == outcomes.NAMESPACE and value['archive_state'] == 'sealed':
            raise RuntimeError('seal acknowledgement lost')
        return original(namespace, key, value, **kwargs)
    monkeypatch.setattr(records, 'put_record', crash)
    with pytest.raises(RuntimeError, match='seal'):
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=terminal(tasks[0]))
    monkeypatch.setattr(records, 'put_record', original)
    manifest = outcomes.read_metadata(run['run_id'], tasks[0]['id'], user_id=OWNER)
    assert manifest['archive_state'] == 'collecting'
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    assert runs.advance_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert outcomes.read_metadata(run['run_id'], tasks[0]['id'], user_id=OWNER)['archive_state'] == 'sealed'
    assert len(tasks) == 2


def test_missing_chunks_defer_live_owner_then_refuse_replay_and_preserve_known_usage(runtime, monkeypatch):
    from lib.evolution import records, stage_outcomes as outcomes
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep terminal facts even if archival fails.'}, user_id=OWNER)
    original = records.put_record
    def crash(namespace, *args, **kwargs):
        if namespace == outcomes.PART_NAMESPACE:
            raise RuntimeError('chunk write interrupted')
        return original(namespace, *args, **kwargs)
    monkeypatch.setattr(records, 'put_record', crash)
    with pytest.raises(RuntimeError, match='chunk'):
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=terminal(tasks[0]))
    monkeypatch.setattr(records, 'put_record', original)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.evolution.review_recovery.owner_state', lambda *args: 'alive')
    assert runs.advance_run(run['run_id'], user_id=OWNER)['status'] == 'executing'
    monkeypatch.setattr('lib.evolution.review_recovery.owner_state', lambda *args: 'dead')
    failed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert failed['status'] == 'needs_attention' and len(tasks) == 1
    assert failed['actuals']['tokens'] == 15
    assert failed.get('actuals_complete') is not False
    assert 'archive is incomplete' in failed['error']
    assert runs.advance_run(run['run_id'], user_id=OWNER)['actuals']['tokens'] == 15
    from lib.evolution.episodes import get_episode
    assert get_episode(run['episode_id'], user_id=OWNER)['actuals']['tokens'] == 15


def test_cancelled_known_terminal_is_settled_without_review_dispatch(runtime, monkeypatch):
    runs, _, tasks = runtime
    run, _ = frozen(runtime, monkeypatch)
    cancelled = runs.cancel_run(run['run_id'], user_id=OWNER)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    settled = runs.advance_run(run['run_id'], user_id=OWNER)
    assert settled['status'] == cancelled['status'] == 'cancelled'
    assert len(tasks) == 1 and settled['actuals']['tokens'] == 15
    from lib.evolution.episodes import get_episode
    assert get_episode(run['episode_id'], user_id=OWNER)['status'] == 'aborted'


@pytest.mark.parametrize('finish', [None, 'length', 'premature_close'])
def test_unverified_delivery_cannot_launch_independent_review(runtime, finish):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Require real terminal evidence.'}, user_id=OWNER)
    task = terminal(tasks[0])
    task['finishReason'] = finish
    failed = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task)
    assert failed['status'] == 'failed' and len(tasks) == 1
    assert failed['review'] is None and 'unverified' in failed['error']
    from lib.evolution.stage_outcomes import page
    original = page(run['run_id'], task['id'], user_id=OWNER)
    assert original['status'] == 'done'
    assert original['terminal_verdict'] == ('truncated' if finish == 'length' else 'failed')
    assert failed['receipts'][0]['terminal_verdict'] == original['terminal_verdict']


def test_partial_actuals_remain_lower_bound_after_restore(runtime, monkeypatch):
    from lib.evolution.stage_outcomes import capture
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Stay inside measured consumption.',
                           'budget': {'max_tokens': 100}}, user_id=OWNER)
    task = terminal(tasks[0])
    task['_executionActualsComplete'] = False
    capture(run, task, user_id=OWNER,
            review_prompt_factory=lambda content: runs._review_prompt({**run, 'result': content}, task))
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    refused = runs.advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and len(tasks) == 1
    assert refused['actuals_complete'] is False and refused['actuals']['tokens'] == 15
    assert refused['error'] == 'execution_token_usage_unknown'


def test_snapshot_and_full_unicode_paging_are_read_only_and_owner_scoped(runtime, monkeypatch):
    from lib.evolution.stage_outcomes import capture, page
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Inspect a long experimental result.'}, user_id=OWNER)
    content = ('真实实验🧪e\u0301\n' * 8000)
    task = terminal(tasks[0], content)
    capture(run, task, user_id=OWNER, review_prompt_factory=lambda text: runs._review_prompt({**run, 'result': text}, task))
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    writes = store.writes
    assert runs.get_run_snapshot(run['run_id'], user_id=OWNER)['status'] == 'settling'
    assert runs.get_run(run['run_id'], user_id=OWNER)['status'] == 'executing'
    chunks, offset = [], 0
    while True:
        result = page(run['run_id'], task['id'], user_id=OWNER, offset=offset)
        assert len(result['content']) <= 6000
        chunks.append(result['content'])
        if result['next_offset'] is None:
            break
        offset = result['next_offset']
    assert ''.join(chunks) == content and store.writes == writes
    assert result['source_sha256'] == hashlib.sha256(content.encode()).hexdigest()
    with pytest.raises(runs.EvolutionStateError):
        page(run['run_id'], task['id'], user_id=OWNER + 1)
    with pytest.raises(ValueError):
        page(run['run_id'], task['id'], user_id=OWNER, offset=-1)
    reviewing = runs.advance_run(run['run_id'], user_id=OWNER)
    assert reviewing['status'] == 'reviewing' and len(tasks) == 2
    assert reviewing['result_source']['total_chars'] == len(content)


@pytest.mark.parametrize('corruption', ['part', 'intent'])
def test_changed_chunk_or_launch_binding_refuses_recovery(runtime, monkeypatch, corruption):
    from lib.evolution import stage_outcomes as outcomes
    runs, store, tasks = runtime
    run, _ = frozen(runtime, monkeypatch)
    if corruption == 'part':
        key = next(key for key in store.rows if key[0] == outcomes.PART_NAMESPACE)
        store.rows[key]['text'] = 'Forged output!!!'
    else:
        key = next(key for key in store.rows if key[0] == outcomes.NAMESPACE)
        store.rows[key]['intent_id'] = 'wrong-intent'
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    refused = runs.advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and len(tasks) == 1
    assert refused['actuals_complete'] is False
    assert 'stage_outcome_recovery_refused' in refused['error']


def test_root_consumption_with_live_child_is_not_complete(runtime):
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.evolution.bridge import actuals_complete
    _, _, tasks = runtime
    # No spawned background task: this tests the common account's lifecycle.
    root = {'id': 'root', 'config': {}, 'usage': {}}
    budget = ExecutionBudget(root)
    root['_executionBudget'] = budget
    child = {'id': 'child', 'usage': {}}
    budget.attach(child)
    assert actuals_complete(root) is False
    budget.close(child)
    assert actuals_complete(root) is True


def test_pending_physical_request_fences_receipt_completeness_even_before_usage_flag():
    from lib.agent_core.execution_budget import ExecutionBudget
    from lib.evolution.bridge import actuals_complete
    root = {'id': 'root-request', 'config': {}, 'usage': {}}
    budget = ExecutionBudget(root)
    root['_executionBudget'] = budget
    lease = budget.reserve_model(root, input_tokens=20, max_output_tokens=30)
    assert actuals_complete(root) is False
    from lib.evolution.bridge import terminal_actuals
    assert terminal_actuals(root)[0]['tokens'] == 0
    lease.mark_dispatched()
    lease.settle({'input_tokens': 20, 'output_tokens': 5})
    observed, complete = terminal_actuals(root)
    assert complete is True and observed['tokens'] == 25
    assert actuals_complete(root) is True


def test_required_delivery_retains_heavy_result_until_archive_retry_succeeds(runtime, monkeypatch):
    from lib.evolution import records, stage_outcomes as outcomes
    from lib.tasks_pkg.manager._persist import persist_task_result
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Do not acknowledge a lost result.'}, user_id=OWNER)
    executor = terminal(tasks[0])
    released = []
    monkeypatch.setattr('lib.tasks_pkg.manager._persist._release_heavy_task_state',
                        lambda task: released.append(task['id']))
    original = records.put_record
    def fail(namespace, *args, **kwargs):
        if namespace == outcomes.PART_NAMESPACE:
            raise RuntimeError('archive temporarily refused')
        return original(namespace, *args, **kwargs)
    monkeypatch.setattr(records, 'put_record', fail)
    assert persist_task_result(executor) is False
    assert executor['_terminalPersistencePending'] is True and released == []
    assert executor['content'] == 'Observed result' and len(tasks) == 1
    monkeypatch.setattr(records, 'put_record', original)
    assert persist_task_result(executor) is True
    assert released == [executor['id']] and len(tasks) == 2
    assert runs.get_run(run['run_id'], user_id=OWNER)['actuals']['tokens'] == 15


def test_committed_review_outcome_resumes_settlement_without_new_review(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep completed independent judgement.'}, user_id=OWNER)
    runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=terminal(tasks[0]))
    reviewer = terminal(tasks[1], review())
    original = runs._save
    def crash(value, *args, **kwargs):
        if len(value['receipts']) == 2:
            raise RuntimeError('review receipt commit interrupted')
        return original(value, *args, **kwargs)
    monkeypatch.setattr(runs, '_save', crash)
    with pytest.raises(RuntimeError, match='review receipt'):
        runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=reviewer)
    monkeypatch.setattr(runs, '_save', original)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    completed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert completed['status'] == 'completed' and len(tasks) == 2
    assert completed['actuals']['tokens'] == 30
    assert completed['receipts'][1]['restored_from_outcome'] is True


def test_terminal_abort_evidence_is_preserved_when_runtime_flags_are_not_restored(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve interruption evidence.'}, user_id=OWNER)
    task = terminal(tasks[0], aborted=True)
    failed = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task)
    assert failed['status'] == 'failed' and len(tasks) == 1
    from lib.evolution.stage_outcomes import read_metadata
    assert read_metadata(run['run_id'], task['id'], user_id=OWNER)['terminal_verdict'] == 'interrupted'


@pytest.mark.parametrize('settlement_failure', [None, 'before_commit', 'after_commit'])
def test_archive_binding_refusal_closes_episode_without_replaying_or_claiming_usage(runtime, monkeypatch, settlement_failure):
    from lib.evolution import stage_outcomes as outcomes, get_episode
    runs, store, tasks = runtime
    run, executor = frozen(runtime, monkeypatch)
    key = outcomes._key(executor['id'], OWNER)
    value, version = outcomes.records.get_versioned_record(outcomes.NAMESPACE, key)
    value['attempt'] += 1
    outcomes.records.put_record(outcomes.NAMESPACE, key, value,
        kind='stage outcome', expected_version=version)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    if settlement_failure:
        original_settle = runs.settle_episode
        def interrupted_settle(*args, **kwargs):
            if settlement_failure == 'after_commit':
                original_settle(*args, **kwargs)
            raise RuntimeError('settlement acknowledgement unavailable')
        monkeypatch.setattr(runs, 'settle_episode', interrupted_settle)
        with pytest.raises(RuntimeError, match='settlement acknowledgement'):
            runs.advance_run(run['run_id'], user_id=OWNER)
        persisted = runs.get_run(run['run_id'], user_id=OWNER)
        assert persisted['status'] == 'settling' and not persisted['actuals_complete']
        assert len(tasks) == 1
        intermediate = get_episode(run['episode_id'], user_id=OWNER)
        assert intermediate['status'] == ('aborted' if settlement_failure == 'after_commit' else 'running')
        monkeypatch.setattr(runs, 'settle_episode', original_settle)
    refused = runs.advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention'
    assert refused['error'].startswith('stage_outcome_recovery_refused:')
    assert len(tasks) == 1 and refused['receipts'] == []
    assert refused['actuals_complete'] is False
    episode = get_episode(run['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['verdict'] is None
    assert episode['actuals_complete'] is False
    writes = store.writes
    repeated = runs.advance_run(run['run_id'], user_id=OWNER)
    assert repeated == refused and store.writes == writes

"""Crash boundaries, durable evidence and authority for evolution stage handoffs."""
from copy import deepcopy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from lib.storage.errors import StorageError
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER

pytestmark = pytest.mark.unit


def test_initial_admission_failure_retains_the_authorized_frozen_request(runtime, monkeypatch):
    from lib.tasks_pkg import manager
    runs, _, tasks = runtime
    original = manager.create_task
    monkeypatch.setattr(manager, 'create_task', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('temporarily unavailable')))
    with pytest.raises(RuntimeError):
        runs.submit_run({'objective': 'Deliver after an admission outage.'}, user_id=OWNER,
                        idempotency_key='admission-outage')
    run_id = 'run-' + hashlib.sha256(b'admission-outage').hexdigest()[:32]
    pending = runs.get_run(run_id, user_id=OWNER)
    assert pending['status'] == 'preparing' and 'stage_admission_deferred' in pending['error']
    assert pending['stage_launch']['stage'] == 'executing' and not tasks
    monkeypatch.setattr(manager, 'create_task', original)
    resumed = runs.advance_run(run_id, user_id=OWNER)
    assert resumed['status'] == 'executing' and resumed['attempt'] == 1 and len(tasks) == 1
    assert resumed['error'] == ''


def interrupt_before_task_allocation(runtime, monkeypatch):
    runs, _, _ = runtime
    original = runs._launch
    def interrupted(*args, **kwargs):
        raise SystemExit('process lost after preparing commit')
    monkeypatch.setattr(runs, '_launch', interrupted)
    return original


def test_review_handoff_restores_exact_observed_evidence_without_reexecuting(runtime, monkeypatch):
    from lib.evolution.stage_launch import read_launch
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Verify a restart-safe notebook.'},
                          user_id=OWNER, idempotency_key='recover-review')
    executor = tasks[0]
    executor['messages'].append({'role': 'tool', 'tool_call_id': 'real-tool-1',
                                'content': 'Observed restart test: 17 assertions passed.'})
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, executor, 'Delivered notebook and reproduction artifacts.')
    pending = runs.get_run(run['run_id'], user_id=OWNER)
    assert pending['status'] == 'preparing' and len(pending['receipts']) == 1
    frozen = read_launch(pending['stage_launch'], run_id=run['run_id'], user_id=OWNER)['prompt']
    assert '17 assertions passed' in frozen
    # The prior transient task and all of its model/tool messages are gone.
    monkeypatch.setattr(runs, 'default_task_loader', lambda tid, user_id: None)
    monkeypatch.setattr(runs, '_review_prompt', lambda *a, **k: pytest.fail('evidence was regenerated'))
    monkeypatch.setattr(runs, '_launch', original)
    restored = runs.advance_run(run['run_id'], user_id=OWNER)
    assert restored['status'] == 'reviewing' and restored['attempt'] == 1
    assert len(tasks) == 2 and tasks[1]['config']['evolution']['stage'] == 'reviewing'
    assert tasks[1]['messages'][0]['content'] == frozen
    assert restored['actuals']['tokens'] == 15
    done = finish(runtime, tasks[1], review())
    assert done['status'] == 'completed' and done['actuals']['tokens'] == 30
    assert len(tasks) == 2


def test_repair_handoff_restores_the_committed_review_and_original_objective(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Deliver the full original objective.', 'max_attempts': 5},
                          user_id=OWNER, idempotency_key='recover-repair')
    finish(runtime, tasks[0], 'Incomplete first deliverable.')
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[1], review('fail'))
    pending = runs.get_run(run['run_id'], user_id=OWNER)
    assert pending['status'] == 'preparing' and pending['stage_launch']['stage'] == 'executing'
    assert pending['review']['outcome'] == 'failure' and len(pending['receipts']) == 2
    monkeypatch.setattr(runs, '_execution_prompt', lambda *a, **k: pytest.fail('repair context was regenerated'))
    monkeypatch.setattr(runs, '_launch', original)
    repaired = runs.advance_run(run['run_id'], user_id=OWNER)
    assert repaired['status'] == 'executing' and repaired['attempt'] == 2
    prompt = tasks[2]['messages'][0]['content']
    assert run['objective'] in prompt and 'Incomplete first deliverable' in prompt and 'failure' in prompt
    assert repaired['actuals']['tokens'] == 30 and len(tasks) == 3


def test_frozen_repair_preserves_maximum_authorized_objective_context_and_requirements(runtime, monkeypatch):
    from lib.evolution.stage_launch import read_launch
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'O' * 4000,
        'acceptance_requirements': ['C' * 600 for _ in range(32)]},
        context_seed='S' * 24000, user_id=OWNER)
    finish(runtime, tasks[0], 'D' * 24000)
    assessment = {'evidence_audit': {'complete': True, 'objections': []},
        'criterion_results': [{'criterion_id': key, 'outcome': 'fail',
        'evidence_ref': 'F' * 500} for key in runs.RUBRIC],
        'requirement_results': [{'requirement_id': f'requirement-{index + 1}',
            'outcome': 'fail', 'evidence_ref': 'E' * 500} for index in range(32)]}
    encoded = json.dumps(assessment)
    assert len(encoded) <= 24000
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[1], encoded)
    pending = runs.get_run(run['run_id'], user_id=OWNER)
    frozen = read_launch(pending['stage_launch'], run_id=run['run_id'], user_id=OWNER)['prompt']
    assert 80000 < len(frozen) <= 200000
    assert 'O' * 4000 in frozen and 'S' * 24000 in frozen and 'D' * 24000 in frozen
    assert all(f'requirement-{index + 1}' in frozen for index in range(32))
    monkeypatch.setattr(runs, '_launch', original)
    repaired = runs.advance_run(run['run_id'], user_id=OWNER)
    assert repaired['status'] == 'executing' and tasks[2]['messages'][0]['content'] == frozen


def test_archive_ack_loss_reuses_the_first_prompt_before_run_pointer_commits(runtime, monkeypatch):
    from lib.evolution import stage_launch
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve the original observed evidence.'}, user_id=OWNER)
    tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'first', 'content': 'First measured evidence.'})
    original_command = store.command
    lost = [False]
    def lose_ack(operation, payload, command_id):
        result = original_command(operation, payload, command_id)
        if payload['namespace'] == stage_launch.NAMESPACE and payload['value']['stage'] == 'reviewing' and not lost[0]:
            lost[0] = True
            raise StorageError('database_timeout', 'lost acknowledged archive response')
        return result
    monkeypatch.setattr(store, 'command', lose_ack)
    with pytest.raises(StorageError): finish(runtime, tasks[0], 'A deliverable.')
    assert runs.get_run(run['run_id'], user_id=OWNER)['status'] == 'executing'
    tasks[0]['messages'] = []
    recovered = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=tasks[0])
    assert recovered['status'] == 'reviewing' and len(tasks) == 2
    assert 'First measured evidence' in tasks[1]['messages'][0]['content']
    assert recovered['actuals']['tokens'] == 15


def test_concurrent_recovery_binds_one_task_and_rejects_the_unstarted_loser(runtime, monkeypatch):
    from threading import Barrier
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Recover one review task.'}, user_id=OWNER)
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[0], 'Deliverable.')
    monkeypatch.setattr(runs, '_launch', original)
    barrier = Barrier(2)
    original_read = runs._read
    def same_generation(*args):
        result = original_read(*args)
        if result[0]['status'] == 'preparing': barrier.wait(timeout=10)
        return result
    monkeypatch.setattr(runs, '_read', same_generation)
    def recover():
        try: return runs.advance_run(run['run_id'], user_id=OWNER)
        except StorageError as exc: return exc.code
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: recover(), range(2)))
    assert all(isinstance(result, dict) and result['status'] == 'reviewing' for result in outcomes)
    assert outcomes[0]['task_id'] == outcomes[1]['task_id'] == tasks[-1]['id']
    assert len(tasks) == 2


@pytest.mark.parametrize('boundary', ['paused_program', 'deadline', 'token_limit'])
def test_program_pause_and_execution_limits_do_not_reauthorize_completed_work(runtime, monkeypatch, boundary):
    from lib.evolution import programs
    runs, _, tasks = runtime
    budget = {'max_tokens': 15} if boundary == 'token_limit' else {}
    if boundary == 'deadline': budget = {'max_wall_clock_seconds': 10}
    program = programs.create_program({'objective': 'Verify the original objective.', 'budget': budget,
        'steps': [{'kind': 'final', 'title': 'Audit', 'instruction': 'Audit the objective.', 'checks': ['Observed evidence exists.']}]},
        user_id=OWNER, idempotency_key='limit-' + boundary)
    program = programs.command_program(program['program_id'], {'action': 'start'}, user_id=OWNER)
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[0], 'Deliverable with measured evidence.')
    monkeypatch.setattr(runs, '_launch', original)
    if boundary == 'deadline':
        monkeypatch.setattr(runs.time, 'time', lambda: program['created_at'] + 11)
    if boundary == 'paused_program':
        programs.command_program(program['program_id'], {'action': 'pause'}, user_id=OWNER)
    after = programs.advance_program(program['program_id'], user_id=OWNER)
    if boundary == 'paused_program':
        assert after['status'] == 'paused' and len(tasks) == 2
        assert tasks[1]['config']['evolution']['stage'] == 'reviewing'
    else:
        assert after['status'] == 'needs_attention' and len(tasks) == 1
        assert 'execution_budget_exhausted' in after['error']


@pytest.mark.parametrize('corruption', ['missing', 'foreign_run', 'source_changed'])
def test_unavailable_or_wrong_stage_evidence_cannot_launch_a_task(runtime, monkeypatch, corruption):
    from lib.evolution import stage_launch
    runs, store, tasks = runtime
    run = runs.submit_run({'objective': 'Use owned frozen evidence.'}, user_id=OWNER)
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[0], 'Deliverable.')
    pending = runs.get_run(run['run_id'], user_id=OWNER)
    key = (stage_launch.NAMESPACE, runs.records.entity_key(pending['stage_launch']['intent_id'], OWNER, kind='stage launch'))
    if corruption == 'missing': del store.rows[key]
    elif corruption == 'foreign_run': store.rows[key]['run_id'] = 'foreign-run'
    else: store.rows[key]['source_task_id'] = 'foreign-task'
    monkeypatch.setattr(runs, '_launch', original)
    refused = runs.advance_run(run['run_id'], user_id=OWNER)
    assert refused['status'] == 'needs_attention' and 'stage_launch_recovery_refused' in refused['error']
    from lib.evolution import get_episode
    assert get_episode(refused['episode_id'], user_id=OWNER)['status'] == 'aborted'
    assert len(tasks) == 1


def test_archive_lifetime_does_not_inflate_the_run_pointer_or_scan_old_stages(runtime):
    from lib.evolution import stage_launch
    runs, store, _ = runtime
    base = {'run_id': 'long-program-run', 'task_id': '', 'attempt': 0, 'receipts': []}
    for attempt in range(400):
        value = deepcopy(base); value['attempt'] = attempt
        reference = stage_launch.freeze_launch(value, 'executing', user_id=OWNER,
                                               prompt_factory=lambda: 'Inspect actual project state. ' * 100)
        assert len(json.dumps(reference)) < 100
    assert len([key for key in store.rows if key[0] == stage_launch.NAMESPACE]) == 400
    first = stage_launch.freeze_launch(base, 'executing', user_id=OWNER,
                                      prompt_factory=lambda: pytest.fail('old prompt rebuilt'))
    assert stage_launch.read_launch(first, run_id=base['run_id'], user_id=OWNER)['prompt'].startswith('Inspect actual')


def test_cancellation_after_preparing_commit_fences_dispatch_and_receipt_replay(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Preserve a cancelled review handoff.'}, user_id=OWNER)
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[0], 'Observed deliverable.')
    pending = runs.get_run(run['run_id'], user_id=OWNER)
    before = deepcopy(pending['receipts'])
    cancelled = runs.cancel_run(run['run_id'], user_id=OWNER)
    from lib.evolution import get_episode
    assert get_episode(cancelled['episode_id'], user_id=OWNER)['status'] == 'aborted'
    monkeypatch.setattr(runs, '_launch', original)
    assert runs.advance_run(run['run_id'], user_id=OWNER)['status'] == 'cancelled'
    replay = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=tasks[0])
    assert replay['status'] == 'cancelled' and replay['receipts'] == before
    assert cancelled['actuals']['tokens'] == 15 and len(tasks) == 1
    episode = get_episode(cancelled['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['actuals']['tokens'] == 15


def test_sweep_recovers_the_handoff_and_does_not_allocate_again_for_live_review(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Recover through the ordinary reaper.'}, user_id=OWNER)
    original = interrupt_before_task_allocation(runtime, monkeypatch)
    with pytest.raises(SystemExit): finish(runtime, tasks[0], 'Delivered artifact.')
    monkeypatch.setattr(runs, '_launch', original)
    transitions = runs.sweep_stale_runs(user_id=OWNER, now=run['created_at'] + 200)
    assert transitions == [{'run_id': run['run_id'], 'from': 'preparing', 'to': 'reviewing'}]
    assert len(tasks) == 2
    assert runs.sweep_stale_runs(user_id=OWNER, now=run['created_at'] + 400) == []
    assert len(tasks) == 2


def test_late_execution_receipt_cannot_close_cancelled_review_before_usage_arrives(runtime, monkeypatch):
    from lib.evolution import get_episode
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep both execution and review usage.'}, user_id=OWNER)
    finish(runtime, tasks[0], 'Delivered artifact.')
    monkeypatch.setattr('lib.tasks_pkg.manager.cancellation.cancel_task', lambda *a, **k: True)
    cancelled = runs.cancel_run(run['run_id'], user_id=OWNER)
    runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=tasks[0])
    assert get_episode(cancelled['episode_id'], user_id=OWNER)['status'] == 'running'
    done = finish(runtime, tasks[1], '', status='aborted')
    assert done['status'] == 'cancelled' and done['actuals']['tokens'] == 30
    episode = get_episode(cancelled['episode_id'], user_id=OWNER)
    assert episode['status'] == 'aborted' and episode['actuals']['tokens'] == 30
    assert runs.sweep_stale_runs(user_id=OWNER, now=run['created_at'] + 400) == []
    assert len(tasks) == 2

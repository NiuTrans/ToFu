"""Fallback retry lifecycle when lossless format projection is unavailable."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from lib.storage.errors import StorageError
from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def unavailable_lossless_projection(monkeypatch):
    # Exercise the unchanged model-rejudge fallback and its failure boundaries.
    monkeypatch.setattr("lib.evolution.runs._normalized_review", lambda *args, **kwargs: None)


def ready(runtime, **options):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Verify all frozen evidence.', 'max_attempts': 1, **options}, user_id=OWNER)
    tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'observed', 'content': 'Observed source trace.'})
    return finish(runtime, tasks[0], 'The frozen deliverable.')


def test_retry_keeps_exact_evidence_and_accounts_each_review_once(runtime):
    from lib.evolution import get_episode, stage_outcomes
    runs, _, tasks = runtime
    old = ready(runtime, budget={'max_tokens': 1000})
    prompt = tasks[-1]['messages'][0]['content']
    malformed = tasks[-1]
    retry = finish(runtime, malformed, 'Full source read.\n' + review())
    assert retry['status'] == 'reviewing' and retry['attempt'] == 1
    assert retry['episode_id'] == old['episode_id'] and retry['stage_launch'] == old['stage_launch']
    assert tasks[-1]['messages'][0]['content'] == prompt and len(tasks) == 3
    assert tasks[-1]['config']['responseFormat'] == {'type': 'json_object'}
    assert tasks[-1]['config']['toolSpecAllowlist'] == []
    assert tasks[-1]['config']['executionLimits']['max_tokens'] == 970
    assert retry['receipts'][-1]['review_format_retry']['admitted'] is True
    assert stage_outcomes.page(old['run_id'], malformed['id'], user_id=OWNER)['content'] == malformed['content']
    completed = finish(runtime, tasks[-1], review())
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 45
    assert len(completed['receipts']) == 3
    assert get_episode(old['episode_id'], user_id=OWNER)['actuals']['tokens'] == 45
    duplicate = runs.advance_run(old['run_id'], user_id=OWNER, terminal_task=malformed)
    assert duplicate == completed and len(tasks) == 3


def test_invalid_json_retry_is_finite_and_separate_from_execution_attempts(runtime):
    _, _, tasks = runtime
    run = ready(runtime)
    for _ in range(3):
        run = finish(runtime, tasks[-1], '{broken')
    assert run['status'] == 'failed' and run['review'] is None
    assert len(tasks) == 4 and run['attempt'] == 1 and run['actuals']['tokens'] == 60
    markers = [r['review_format_retry'] for r in run['receipts'] if 'review_format_retry' in r]
    assert [m['admitted'] for m in markers] == [True, True, False]
    assert markers[-1]['refusal'] == 'retry_budget'


@pytest.mark.parametrize('case', ['disabled', 'legacy', 'tokens', 'unknown', 'deadline', 'missing_archive'])
def test_refusal_preserves_output_and_launches_no_reviewer(runtime, case):
    runs, store, tasks = runtime
    options = {'max_review_format_retries': 0} if case == 'disabled' else {'budget': {'max_tokens': 30}} if case == 'tokens' else {}
    run = ready(runtime, **options)
    if case in ('legacy', 'deadline'):
        saved, version = runs._read(run['run_id'], OWNER)
        if case == 'legacy': saved.pop('max_review_format_retries')
        else: saved['absolute_deadline_at'] = 1
        runs._save(saved, version, OWNER)
    if case == 'missing_archive':
        from lib.evolution import stage_launch
        key = stage_launch.records.entity_key(run['stage_launch']['intent_id'], OWNER, kind='stage launch')
        store.rows.pop((stage_launch.NAMESPACE, key))
    if case == 'unknown':
        saved, version = runs._read(run['run_id'], OWNER)
        saved['budget'] = {'max_tokens': 1000, 'max_tool_calls': 0, 'max_wall_clock_seconds': 0}
        saved['actuals_complete'] = False
        runs._save(saved, version, OWNER)
    final = finish(runtime, tasks[-1], 'not JSON')
    assert final['status'] == 'failed' and len(tasks) == 2 and final['actuals']['tokens'] == 30
    assert final['receipts'][-1]['review_format_retry']['admitted'] is False


@pytest.mark.parametrize('output', [review('fail'), review('unknown'), '{}', '[]'])
def test_valid_json_negative_or_invalid_contract_is_not_format_retried(runtime, output):
    _, _, tasks = runtime
    ready(runtime)
    final = finish(runtime, tasks[-1], output)
    assert final['status'] in ('needs_attention', 'failed') and len(tasks) == 2
    assert 'review_format_retry' not in final['receipts'][-1]


def test_terminal_failure_and_output_bound_are_not_format_retry_authority(runtime):
    _, _, tasks = runtime
    ready(runtime)
    final = finish(runtime, tasks[-1], 'x' * 8001)
    assert final['status'] == 'failed' and len(tasks) == 2
    assert 'review_format_retry' not in final['receipts'][-1]


def pause_admission(runtime, monkeypatch):
    runs = runtime[0]
    launch = runs._launch
    monkeypatch.setattr(runs, '_launch', lambda run, version, uid, **kw: runs.get_run(run['run_id'], user_id=uid))
    return launch


@pytest.mark.parametrize('kind', ['syntax', 'text_limit'])
def test_pending_retry_survives_ack_loss_and_sweep_without_executor_replay(runtime, monkeypatch, kind):
    runs, _, tasks = runtime
    run = ready(runtime)
    original = runs._save
    def lost_ack(value, version, user_id):
        result = original(value, version, user_id)
        if value['status'] == 'preparing' and value['receipts'][-1].get('review_format_retry'):
            raise StorageError('database_timeout', 'lost acknowledgement after committed retry')
        return result
    monkeypatch.setattr(runs, '_save', lost_ack)
    output = 'preface' + review() if kind == 'syntax' else oversized_review()
    with pytest.raises(StorageError): finish(runtime, tasks[-1], output)
    monkeypatch.setattr(runs, '_save', original)
    assert runs.get_run(run['run_id'], user_id=OWNER)['status'] == 'preparing'
    runs.sweep_stale_runs(user_id=OWNER, grace_seconds=0)
    restored = runs.get_run(run['run_id'], user_id=OWNER)
    assert restored['status'] == 'reviewing' and len(tasks) == 3
    assert restored['episode_id'] == run['episode_id'] and restored['attempt'] == 1
    assert finish(runtime, tasks[-1], review())['status'] == 'completed'


@pytest.mark.parametrize('kind', ['syntax', 'text_limit'])
def test_cancel_committed_retry_does_not_dispatch_and_preserves_cost(runtime, monkeypatch, kind):
    from lib.evolution import get_episode
    runs, _, tasks = runtime
    run = ready(runtime)
    launch = pause_admission(runtime, monkeypatch)
    prepared = finish(runtime, tasks[-1], '{broken' if kind == 'syntax' else oversized_review())
    assert prepared['status'] == 'preparing'
    monkeypatch.setattr(runs, '_launch', launch)
    cancelled = runs.cancel_run(run['run_id'], user_id=OWNER)
    runs.advance_run(run['run_id'], user_id=OWNER)
    assert cancelled['status'] == 'cancelled' and len(tasks) == 2
    ep = get_episode(run['episode_id'], user_id=OWNER)
    assert ep['status'] == 'aborted' and ep['actuals']['tokens'] == 30


@pytest.mark.parametrize('kind', ['syntax', 'text_limit'])
def test_two_terminal_consumers_dispatch_one_review_successor(runtime, kind):
    runs, _, tasks = runtime
    run = ready(runtime)
    task = tasks[-1]
    output = '{broken' if kind == 'syntax' else oversized_review()
    task.update(status='done', finishReason='stop', content=output, finished_at=task['created_at'] + 1,
                usage={'input_tokens': 10, 'output_tokens': 5})
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(runs.advance_run, run['run_id'], user_id=OWNER, terminal_task=task) for _ in range(2)]
        results = [f.result() for f in futures]
    assert all(r['status'] in ('preparing', 'reviewing') for r in results) and len(tasks) == 3
    assert runs.get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert runs.get_run(run['run_id'], user_id=OWNER)['actuals']['tokens'] == 30


def test_policy_is_idempotent_and_program_override_is_inherited(runtime):
    from lib.evolution import programs
    runs, _, _ = runtime
    request = {'objective': 'Keep original', 'max_review_format_retries': 1}
    run = runs.submit_run(request, user_id=OWNER, idempotency_key='format-policy')
    assert runs.submit_run(request, user_id=OWNER, idempotency_key='format-policy') == run
    with pytest.raises(runs.EvolutionStateError):
        runs.submit_run({**request, 'max_review_format_retries': 2}, user_id=OWNER, idempotency_key='format-policy')
    program = programs.create_program({'objective': 'Explore safely', 'max_review_format_retries': 0}, user_id=OWNER, idempotency_key='format-program')
    assert program['execution_options']['max_review_format_retries'] == 0
    programs.command_program(program['program_id'], {'action': 'start'}, user_id=OWNER)
    active = programs.advance_program(program['program_id'], user_id=OWNER)
    active = programs.advance_program(program['program_id'], user_id=OWNER)
    current = runs.get_run(active['current']['run_id'], user_id=OWNER)
    assert current['max_review_format_retries'] == 0


@pytest.mark.parametrize('value', [-1, 101, True, '2', None])
def test_invalid_policy_is_rejected_before_execution(runtime, value):
    runs, _, tasks = runtime
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'Policy validation', 'max_review_format_retries': value}, user_id=OWNER)
    assert not tasks


def test_failed_terminal_delivery_has_no_format_retry_authority(runtime):
    _, _, tasks = runtime
    ready(runtime)
    final = finish(runtime, tasks[-1], '{broken', status='error')
    assert final['status'] == 'failed' and len(tasks) == 2
    assert 'review_format_retry' not in final['receipts'][-1]


def test_retry_requires_fresh_full_source_reads(runtime):
    _, _, tasks = runtime
    ready(runtime)
    # Use a separate run with an actually archived source exceeding its preview.
    runs = runtime[0]
    run = runs.submit_run({'objective': 'Audit the complete original', 'max_attempts': 1}, user_id=OWNER)
    executor = tasks[-1]
    executor['messages'].append({'role': 'tool', 'tool_call_id': 'original', 'content': 'x' * 8000})
    reviewing = finish(runtime, executor, 'Audit claim')
    source = reviewing['tool_evidence_source']
    old_review = tasks[-1]
    old_review['_evolutionToolEvidenceCoverage'] = {'source': source, 'ranges': [[0, source['total_chars']]]}
    retry = finish(runtime, old_review, 'preface' + review())
    assert retry['status'] == 'reviewing'
    assert tasks[-1]['config']['toolSpecAllowlist'] == ['evolution_source', 'evolution_review']
    assert '_evolutionToolEvidenceCoverage' not in tasks[-1]
    rejected = finish(runtime, tasks[-1], review())
    assert rejected['status'] == 'needs_attention'
    assert next(item for item in rejected['review']['criterion_results'] if item['criterion_id'] == 'objective')['outcome'] == 'unknown'


def test_legacy_review_without_frozen_intent_stays_terminal(runtime):
    runs, _, tasks = runtime
    run = ready(runtime)
    saved, version = runs._read(run['run_id'], OWNER)
    saved.pop('stage_launch');saved.pop('max_review_format_retries')
    runs._save(saved, version, OWNER)
    final = finish(runtime, tasks[-1], '{broken')
    assert final['status'] == 'failed' and len(tasks) == 2
    assert final['receipts'][-1]['review_format_retry']['refusal'] == 'frozen_evidence_unavailable'


def test_deadline_expiring_after_retry_commit_refuses_dispatch(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = ready(runtime)
    launch = pause_admission(runtime, monkeypatch)
    prepared = finish(runtime, tasks[-1], '{broken')
    saved, version = runs._read(run['run_id'], OWNER)
    saved['absolute_deadline_at'] = 1;runs._save(saved, version, OWNER)
    monkeypatch.setattr(runs, '_launch', launch)
    stopped = runs.advance_run(run['run_id'], user_id=OWNER)
    assert stopped['status'] == 'needs_attention' and stopped['error'] == 'execution_budget_exhausted'
    assert len(tasks) == 2 and stopped['actuals']['tokens'] == 30


def test_format_allowance_resets_only_after_a_new_execution_episode(runtime):
    runs, _, tasks = runtime
    ready(runtime, max_attempts=2, max_review_format_retries=1)
    first = finish(runtime, tasks[-1], '{broken')
    repair = finish(runtime, tasks[-1], review('fail'))
    assert repair['status'] == 'executing' and repair['episode_id'] != first['episode_id']
    second = finish(runtime, tasks[-1], 'A new delivery with independent evidence.')
    retry = finish(runtime, tasks[-1], '{broken')
    assert retry['status'] == 'reviewing' and retry['attempt'] == 2
    assert retry['episode_id'] == second['episode_id']
    completed = finish(runtime, tasks[-1], review())
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 90
    markers = [r['review_format_retry'] for r in completed['receipts'] if r.get('review_format_retry')]
    assert len(markers) == 2 and all(m['admitted'] for m in markers)


def oversized_review(field='criterion', outcome='pass'):
    value = json.loads(review(outcome))
    if field == 'criterion':
        value['criterion_results'][0]['evidence_ref'] = 'x' * 524
    elif field == 'requirement':
        value['requirement_results'] = [{'requirement_id': 'requirement-1',
            'outcome': outcome, 'evidence_ref': 'x' * 524}]
    else:
        value['consumer_assessment'] = {'meaningful_to_consumer': 'unclear',
                                         'note': 'x' * 324}
    return json.dumps(value)


@pytest.mark.parametrize('field', ['criterion', 'requirement', 'consumer'])
def test_overlong_passing_review_retries_with_original_sources_and_exact_cost(runtime, field):
    from lib.evolution import stage_outcomes
    runs, _, tasks = runtime
    options = {'acceptance_requirements': ['Observe source evidence.']} if field == 'requirement' else {}
    old = ready(runtime, budget={'max_tokens': 1000}, **options)
    prompt = tasks[-1]['messages'][0]['content']
    malformed = tasks[-1]
    output = oversized_review(field)
    retry = finish(runtime, malformed, output)
    assert retry['status'] == 'reviewing' and len(tasks) == 3
    assert retry['episode_id'] == old['episode_id'] and retry['attempt'] == 1
    assert retry['stage_launch'] == old['stage_launch']
    assert tasks[-1]['messages'][0]['content'] == prompt
    assert tasks[-1]['config']['toolSpecAllowlist'] == []
    assert tasks[-1]['config']['executionLimits']['max_tokens'] == 970
    assert retry['receipts'][-1]['review_format_retry']['error'] == 'text_limit'
    assert stage_outcomes.page(old['run_id'], malformed['id'], user_id=OWNER)['content'] == output
    corrected = json.loads(review())
    if options:
        corrected['requirement_results'] = [{'requirement_id': 'requirement-1',
            'outcome': 'pass', 'evidence_ref': 'Observed source evidence.'}]
    completed = finish(runtime, tasks[-1], json.dumps(corrected))
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 45
    assert runs.advance_run(old['run_id'], user_id=OWNER, terminal_task=malformed) == completed
    assert len(tasks) == 3


@pytest.mark.parametrize('field', ['criterion', 'requirement', 'consumer'])
@pytest.mark.parametrize('outcome', ['fail', 'unknown'])
def test_overlong_text_never_rerolls_a_negative_or_unknown_verdict(runtime, field, outcome):
    _, _, tasks = runtime
    options = {'acceptance_requirements': ['Observe source evidence.']} if field == 'requirement' else {}
    ready(runtime, **options)
    final = finish(runtime, tasks[-1], oversized_review(field, outcome))
    assert final['status'] == 'failed' and len(tasks) == 2
    assert 'review_format_retry' not in final['receipts'][-1]


@pytest.mark.parametrize('case', ['missing_requirement', 'duplicate_criterion', 'invalid_outcome', 'unknown_field'])
def test_length_violation_does_not_hide_other_contract_or_audit_failures(runtime, case):
    _, _, tasks = runtime
    ready(runtime, acceptance_requirements=['Observe source evidence.'])
    value = json.loads(oversized_review())
    value['requirement_results'] = [{'requirement_id': 'requirement-1',
                                    'outcome': 'pass', 'evidence_ref': 'Observed.'}]
    if case == 'missing_requirement': value.pop('requirement_results')
    elif case == 'duplicate_criterion': value['criterion_results'][1]['criterion_id'] = 'objective'
    elif case == 'invalid_outcome': value['criterion_results'][1]['outcome'] = 'probably'
    else: value['extra'] = True
    final = finish(runtime, tasks[-1], json.dumps(value))
    assert final['status'] == 'failed' and len(tasks) == 2
    assert 'review_format_retry' not in final['receipts'][-1]


def test_syntax_and_text_limit_retries_share_one_finite_allowance(runtime):
    _, _, tasks = runtime
    ready(runtime, max_review_format_retries=2)
    first = finish(runtime, tasks[-1], '{broken')
    second = finish(runtime, tasks[-1], oversized_review())
    final = finish(runtime, tasks[-1], oversized_review())
    assert first['status'] == second['status'] == 'reviewing'
    assert final['status'] == 'failed' and len(tasks) == 4
    markers = [row['review_format_retry'] for row in final['receipts'] if row.get('review_format_retry')]
    assert [m['error'] for m in markers] == ['invalid_json', 'text_limit', 'text_limit']
    assert [m['admitted'] for m in markers] == [True, True, False]
    assert markers[-1]['refusal'] == 'retry_budget'
    assert final['actuals']['tokens'] == 60


@pytest.mark.parametrize('case', ['disabled', 'tokens', 'deadline'])
def test_text_limit_recovery_obeys_existing_policy_and_budget_fences(runtime, case):
    runs, _, tasks = runtime
    options = {'max_review_format_retries': 0} if case == 'disabled' else {'budget': {'max_tokens': 30}} if case == 'tokens' else {}
    run = ready(runtime, **options)
    if case == 'deadline':
        saved, version = runs._read(run['run_id'], OWNER)
        saved['absolute_deadline_at'] = 1
        runs._save(saved, version, OWNER)
    final = finish(runtime, tasks[-1], oversized_review())
    assert final['status'] == 'failed' and len(tasks) == 2
    marker = final['receipts'][-1]['review_format_retry']
    assert marker['error'] == 'text_limit' and marker['admitted'] is False


def test_text_limit_retry_requires_new_complete_source_coverage(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Audit complete evidence.', 'max_attempts': 1}, user_id=OWNER)
    tasks[-1]['messages'].append({'role': 'tool', 'tool_call_id': 'source', 'content': 'x' * 8000})
    old = finish(runtime, tasks[-1], 'Observed complete source.')
    source = old['tool_evidence_source']
    tasks[-1]['_evolutionToolEvidenceCoverage'] = {'source': source, 'ranges': [[0, source['total_chars']]]}
    retry = finish(runtime, tasks[-1], oversized_review())
    assert retry['status'] == 'reviewing' and tasks[-1]['config']['toolSpecAllowlist'] == ['evolution_source', 'evolution_review']
    assert '_evolutionToolEvidenceCoverage' not in tasks[-1]
    final = finish(runtime, tasks[-1], review())
    assert final['status'] == 'needs_attention'
    assert final['review']['criterion_results'][0]['outcome'] == 'unknown'

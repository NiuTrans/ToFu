"""Durability, acceptance and recovery boundaries for long-horizon exploration."""
from copy import deepcopy
import hashlib
import json

import pytest
from tests.evolution.test_evolution_runs import runtime as _task_runtime_fixture, finish as finish_task
task_runtime = _task_runtime_fixture

from lib.storage.errors import StorageError
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit
OWNER = 512


def test_complete_program_catalog_uses_bounded_owner_ranges_and_bidirectional_cursors(runtime):
    programs, store, launched = runtime
    ids = [programs.create_program({'objective': f'Long exploration {index}'},
        user_id=OWNER, idempotency_key=f'catalog-{index}')['program_id'] for index in range(73)]
    reads = []
    query = store.query
    def measured(operation, payload):
        if operation == 'record.list':
            reads.append(deepcopy(payload))
        return query(operation, payload)
    store.query = measured
    writes = store.writes
    after = ''
    pages = []
    for _ in range(20):
        page = programs.program_page(user_id=OWNER, after=after, limit=7)
        pages.append(page)
        assert page['order'] == 'program_id_asc'
        assert all(set(row) == {'program_id', 'objective', 'status', 'settled_through',
            'pending_steps', 'current_title', 'created_at', 'updated_at'} for row in page['programs'])
        if page['next_cursor'] is None:
            break
        assert page['next_cursor'] != after
        after = page['next_cursor']
    assert [row['program_id'] for page in pages for row in page['programs']] == sorted(ids)
    assert pages[0]['previous_cursor'] is None and pages[-1]['next_cursor'] is None
    assert len(reads) == len(pages) and all(read['limit'] == 8 for read in reads)
    for index in range(len(pages)-1, 0, -1):
        previous = programs.program_page(user_id=OWNER, before=pages[index]['previous_cursor'], limit=7)
        assert previous['programs'] == pages[index-1]['programs']
    assert programs.program_page(user_id=OWNER+1)['programs'] == []
    assert store.writes == writes and launched == []


@pytest.mark.parametrize('query', [{'limit': 0}, {'limit': 51}, {'limit': True},
    {'after': 'bad:cursor'}, {'after': 'a', 'before': 'b'}, {'before': 'x'*161}])
def test_invalid_catalog_query_never_reads_storage(runtime, query):
    programs, store, _ = runtime
    store.query = lambda *args: pytest.fail('Invalid cursor must fail before storage')
    with pytest.raises(programs.EvolutionContractError):
        programs.program_page(user_id=OWNER, **query)


def test_catalog_rejects_foreign_or_nonadvancing_storage_rows(runtime):
    programs, store, _ = runtime
    value = create(runtime)
    for keys in [[f'{OWNER+1}:{value["program_id"]}'],
                 [f'{OWNER}:{value["program_id"]}']*2]:
        store.query = lambda *args: [{'key': key, 'value': deepcopy(value)} for key in keys]
        with pytest.raises(StorageError, match='record page boundary'):
            programs.program_page(user_id=OWNER)


@pytest.mark.parametrize('error_message', ['versioned document page exceeds 8 MiB', 'database_resource_exhausted'])
def test_catalog_byte_overflow_reduces_page_at_same_cursor_without_losing_projects(runtime, error_message):
    programs, store, _ = runtime
    ids = sorted(programs.create_program({'objective': f'Large original {index}'},
        user_id=OWNER, idempotency_key=f'large-catalog-{index}')['program_id'] for index in range(7))
    query = store.query
    reads = []
    def bounded(operation, payload):
        reads.append(deepcopy(payload))
        if payload['limit'] > 3:
            raise StorageError('database_unavailable', error_message)
        return query(operation, payload)
    store.query = bounded
    after = ''; seen = []
    for _ in range(4):
        first_read = len(reads)
        page = programs.program_page(user_id=OWNER, after=after)
        attempts = reads[first_read:]
        assert [read['limit'] for read in attempts] == [21,11,6,3]
        assert len({read['after_key'] for read in attempts}) == 1
        seen.extend(row['program_id'] for row in page['programs'])
        if page['next_cursor'] is None:
            break
        after = page['next_cursor']
    assert seen == ids


def step(kind='experiment', title='Measure an alternative'):
    return {'kind': kind, 'title': title, 'instruction': 'Run the measured protocol.',
            'checks': ['Preserve the measured output and reproduction command.']}


@pytest.fixture
def runtime(monkeypatch):
    from tests.support.evolution_store import install_store
    from lib.evolution import programs
    store = install_store(monkeypatch)
    launched = []
    def submit(payload, *, user_id, idempotency_key, context_seed, deadline_at=None, exploration_hypothesis=None):
        rid = 'run-' + hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
        previous = programs.runs.get_run(rid, user_id=user_id)
        if previous:
            return previous
        launched.append({'payload': deepcopy(payload), 'seed': context_seed,
                         'request_id': idempotency_key, 'run_id': rid,
                         'exploration_hypothesis': deepcopy(exploration_hypothesis)})
        run = {'run_id': rid, 'status': 'executing', 'result': '', 'review': None,
               'objective':payload['objective'],'acceptance_requirements':deepcopy(payload.get('acceptance_requirements',[])),
               **({'require_requirement_originals':True} if payload.get('require_requirement_originals') else {}),
               **{key:deepcopy(payload[key]) for key in ('scientific_protocol','scientific_required_outcome','require_command_execution') if key in payload},
               'actuals': {'tokens': 0, 'wall_clock_seconds': 0, 'tool_calls': 0},
               'updated_at': 1, 'error': ''}
        programs.records.put_record(programs.records.NAMESPACE_RUNS,
            programs.records.entity_key(rid, user_id, kind='run'), run, kind='run', expected_version=0)
        return run
    monkeypatch.setattr(programs.runs, 'submit_run', submit)
    monkeypatch.setattr(programs.runs, 'advance_run',
                        lambda rid, user_id: programs.runs.get_run(rid, user_id=user_id))
    def cancel(rid, *, user_id):
        run = programs.runs.get_run(rid, user_id=user_id)
        run['status'] = 'cancelled'
        # This scheduler-only fake has synchronously drained its task. Real
        # late receipts are exercised by the cancellation accounting tests.
        run['task_id'] = rid
        run['receipts'] = [{'task_id': rid, 'stage': 'executing',
                            'actuals': dict(run['actuals'])}]
        programs.records.put_record(programs.records.NAMESPACE_RUNS,
            programs.records.entity_key(rid, user_id, kind='run'), run, kind='run')
        return run
    monkeypatch.setattr(programs.runs, 'cancel_run', cancel)
    return programs, store, launched


def create(runtime, request_id='program-test', **overrides):
    programs, _, _ = runtime
    return programs.create_program({'objective': 'Build a restart-safe usable studio.',
        'acceptance': ['Restart preserves the experiment history.', 'The user can pause exploration.'],
        'steps': [step(), step('final', 'Audit the original objective')], **overrides},
        user_id=OWNER, idempotency_key=request_id)


def command(runtime, program, action):
    return runtime[0].command_program(program['program_id'], {'action': action}, user_id=OWNER)


def launch(runtime, program):
    command(runtime, program, 'resume')
    intent = command(runtime, program, 'advance')
    assert runtime[2] == [] or intent['current']
    return command(runtime, program, 'advance')


def finish(runtime, program, status='completed', result='Observed successful checks'):
    programs, _, _ = runtime
    rid = program['current']['run_id']
    run = programs.runs.get_run(rid, user_id=OWNER)
    run.update(status=status, result=result, actuals={'tokens': 23, 'wall_clock_seconds': 4, 'tool_calls': 3},
               review={'outcome': 'success' if status == 'completed' else 'failure'},
               error='' if status == 'completed' else 'Missing restart evidence')
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(rid, OWNER, kind='run'), run, kind='run')
    return command(runtime, program, 'advance')


def test_default_phase_context_preserves_current_checks_and_original_goal(runtime):
    programs, _, launched = runtime
    program = programs.create_program({
        'objective': 'Build a restart-safe usable studio.',
        'acceptance': ['Restart preserves the experiment history.',
                       'The user can pause exploration.'],
    }, user_id=OWNER, idempotency_key='default-phase-context')
    program = launch(runtime, program)
    original_objective = program['objective']
    original_acceptance = deepcopy(program['acceptance'])
    for kind in ['discovery', 'baseline', 'experiment', 'synthesis', 'experiment', 'final']:
        current = program['current']
        seed = json.loads(launched[-1]['seed'])
        assert seed['current_experiment'] == {key: current['step'][key]
                                             for key in ('kind', 'title')}
        required = current['step']['checks'] + ([] if kind != 'final' else original_acceptance)
        assert set(launched[-1]['payload']['acceptance_requirements']) == set(required)
        assert launched[-1]['payload'].get('require_command_execution') == current['step'].get('require_command_execution')
        assert seed['current_experiment']['kind'] == kind
        assert seed['original_objective'] == original_objective
        assert seed['original_acceptance'] == original_acceptance
        frozen = current['context_seed']
        # Reconciliation reuses the same staged context rather than widening
        # this experiment to the final audit's work.
        program = command(runtime, program, 'advance')
        assert program['current']['context_seed'] == frozen
        if kind=='synthesis':
            from tests.evolution.test_evolution_scientific_observations import protocol
            target=launched[-1]['payload']['validation_preregistration_target']
            from tests.support.research_observations import synthetic_frozen_scope
            value = protocol(); value['measurement_scope'] = synthetic_frozen_scope(value)
            programs.preregister_validation(program['program_id'],target['target_step_sha256'],value,'supported',user_id=OWNER,executor_run_id=program['current']['run_id'])
        program = finish(runtime, program)
        if kind != 'final':
            program = launch(runtime, program)
    assert program['status'] == 'completed' and program['settled_through'] == 6


def test_large_check_sets_stay_in_payload_without_inflating_context(runtime):
    programs, _, launched = runtime
    checks = [str(i) + 'x' * 598 for i in range(16)]
    acceptance = [str(i) + 'y' * 598 for i in range(16)]
    program = programs.create_program({
        'objective': 'z' * 4000, 'acceptance': acceptance,
        'steps': [dict(step('discovery'), checks=checks), step('final')],
    }, user_id=OWNER, idempotency_key='large-phase-context')
    program = launch(runtime, program)
    seed = json.loads(launched[0]['seed'])
    assert len(launched[0]['seed']) <= 24000
    assert seed['original_acceptance'] == acceptance
    assert launched[0]['payload']['acceptance_requirements'] == checks
    assert seed['current_experiment']['kind'] == 'discovery'


def context_executor(runtime, program):
    programs = runtime[0];run_id = program['current']['run_id']
    run = programs.runs.get_run(run_id, user_id=OWNER);run['task_id'] = 'context-executor'
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(run_id, OWNER, kind='run'), run, kind='run')
    return {'id': 'context-executor', '_userId': OWNER,
            'config': {'evolution': {'run_id': run_id, 'stage': 'executing'}}}


def read_context(task, program, **args):
    from lib.evolution.exploration_tools import execute
    return execute(task, {'program_id': program['program_id'], 'action': 'read_context', **args},
                   call_id='context-test')


def test_complex_original_requirements_and_future_hypothesis_remain_complete_beyond_former_seed_limit(runtime):
    proposed = step()
    hypothesis = {};proposed['hypothesis'] = hypothesis
    for key in ('problem', 'audience', 'baseline', 'scenario', 'mechanism', 'expected_value', 'falsifier'):
        hypothesis[key] = key + '条件' * ((1000-len(key))//2)
    hypothesis['assumptions'] = ['条件尚未验证 ' + 'x'*591 for _ in range(8)]
    hypothesis['evidence_refs'] = ['source:' + 'y'*593 for _ in range(8)]
    objective = '目标' + '\n值' * 1999
    acceptance = ['验收' + '\t值' * 299 for _ in range(16)]
    program = launch(runtime, create(runtime, objective=objective, acceptance=acceptance,
        steps=[proposed, step('final')]))
    seed = json.loads(program['current']['context_seed'])
    assert len(program['current']['context_seed']) > 24000
    assert seed['original_objective'] == objective
    assert seed['original_acceptance'] == acceptance
    assert seed['current_hypothesis'] == hypothesis
    assert 'current_context_source' not in seed
    task = context_executor(runtime, program)
    ref = read_context(task, program, offset=0)
    pages = []; offset = 0; writes = runtime[1].writes
    while True:
        page = read_context(task, program, offset=offset, source_digest=ref['source_digest'])
        assert page['source_digest'] == ref['source_digest'] and len(page['content']) <= 6000
        pages.append(page['content'])
        if page['next_offset'] is None: break
        offset = page['next_offset']
    original = json.loads(''.join(pages))
    assert original['original_objective'] == objective and original['original_acceptance'] == acceptance
    assert original['current_hypothesis'] == hypothesis
    assert runtime[1].writes == writes and len(runtime[2]) == 1
    assert runtime[0].get_program(program['program_id'], user_id=OWNER)['current'] == program['current']


def test_escaped_large_history_cannot_block_next_stage_or_erase_negative_receipts(runtime):
    programs, store, _ = runtime
    program = launch(runtime, create(runtime, objective='目标'*2000,
        acceptance=['验收'+'x'*598 for _ in range(16)]))
    program = finish(runtime, program)
    receipt = programs.experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]
    for sequence in range(1, 7):
        archived = {**receipt, 'sequence': sequence, 'result': '真实反例' + '\t'*23996,
            'error': '错误' + '\n'*998, 'outcome': 'failed'}
        from lib.evolution.contracts import validate_program
        validate_program(archived, 'receipt')
        store.rows[(programs.EXPERIMENTS, programs._experiment_key(program['program_id'], sequence, OWNER))] = archived
    header, version = programs._read(program['program_id'], OWNER)
    header['settled_through'] = 6;programs._save(header, version, OWNER)
    intent = programs.advance_program(program['program_id'], user_id=OWNER)['current']
    seed = json.loads(intent['context_seed'])
    assert len(intent['context_seed']) > 24000 and seed['original_objective'] == header['objective']
    assert seed['original_acceptance'] == header['acceptance']
    assert [row['sequence'] for row in seed['recent_experiments']] == list(range(1, 7))
    assert all(row['outcome'] == 'failed' and row['preview_truncated'] and row['error'] == '错误' + '\n'*998
        for row in seed['recent_experiments'])
    assert len(programs.experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]['result']) == 24000


def test_original_objective_final_audit_and_history_survive_fresh_service(runtime):
    programs, _, launched = runtime
    program = launch(runtime, create(runtime))
    settled = finish(runtime, program)
    assert settled['status'] == 'active' and settled['settled_through'] == 1
    assert settled['actuals']['tokens'] == 23
    # There is no process-local program state: the next call rebuilds from records.
    command(runtime, settled, 'advance')
    final = command(runtime, settled, 'advance')
    assert len(launched) == 2
    assert launched[-1]['payload']['objective'] == program['objective']
    assert launched[-1]['payload']['acceptance_requirements'][:2] == program['acceptance']
    assert json.loads(launched[-1]['seed'])['recent_experiments'][0]['run_id'] == launched[0]['run_id']
    completed = finish(runtime, final)
    assert completed['status'] == 'completed' and completed['settled_through'] == 2
    assert completed['actuals']['tokens'] == 46
    page = programs.experiment_page(program['program_id'], user_id=OWNER, limit=1)
    assert len(page['experiments']) == 1 and page['next_cursor'] == 1
    next_page = programs.experiment_page(program['program_id'], user_id=OWNER, after=1)
    assert next_page['experiments'][0]['step']['kind'] == 'final' and next_page['next_cursor'] is None


def test_request_identity_and_owner_are_durable_boundaries(runtime):
    program = create(runtime)
    assert create(runtime) == program
    with pytest.raises(runtime[0].EvolutionStateError):
        create(runtime, objective='Different objective')
    with pytest.raises(runtime[0].EvolutionStateError):
        runtime[0].get_program(program['program_id'], user_id=OWNER + 1)
    assert runtime[0].list_programs(user_id=OWNER + 1) == []
    with pytest.raises(runtime[0].EvolutionContractError):
        create(runtime, request_id='')


def test_pause_settles_current_work_but_launches_no_successor(runtime):
    program = launch(runtime, create(runtime))
    command(runtime, program, 'pause')
    settled = finish(runtime, program)
    assert settled['status'] == 'paused' and settled['settled_through'] == 1
    command(runtime, settled, 'advance')
    assert len(runtime[2]) == 1
    command(runtime, settled, 'resume')
    command(runtime, settled, 'advance')
    command(runtime, settled, 'advance')
    assert len(runtime[2]) == 2


def test_cancelled_launch_intent_never_starts_work(runtime):
    program = create(runtime)
    command(runtime, program, 'resume')
    intent = command(runtime, program, 'advance')
    assert intent['current'] and not runtime[2]
    cancelled = command(runtime, program, 'cancel')
    command(runtime, cancelled, 'advance')
    assert cancelled['status'] == 'cancelled' and not runtime[2]


def test_cancellation_reaches_the_current_run(runtime):
    program = launch(runtime, create(runtime))
    command(runtime, program, 'cancel')
    assert runtime[0].runs.get_run(program['current']['run_id'], user_id=OWNER)['status'] == 'cancelled'
    command(runtime, program, 'advance')
    assert len(runtime[2]) == 1


def test_ambiguous_launch_response_reuses_frozen_request(runtime, monkeypatch):
    programs, _, launched = runtime
    program = create(runtime)
    command(runtime, program, 'resume')
    intent = command(runtime, program, 'advance')
    original = programs.runs.submit_run
    def ambiguous(*args, **kwargs):
        original(*args, **kwargs)
        raise StorageError('database_timeout', 'response lost after commit')
    monkeypatch.setattr(programs.runs, 'submit_run', ambiguous)
    with pytest.raises(StorageError):
        command(runtime, program, 'advance')
    monkeypatch.setattr(programs.runs, 'submit_run', original)
    command(runtime, program, 'advance')
    assert len(launched) == 1
    assert programs.get_program(program['program_id'], user_id=OWNER)['current'] == intent['current']


def test_competing_run_admission_reuses_the_frozen_launch_inputs(task_runtime, monkeypatch):
    from lib.evolution import programs
    runs, _, tasks = task_runtime
    owner = 123
    monkeypatch.setattr(programs.time, 'time', lambda: 1000)
    program = programs.create_program({
        'objective': 'Preserve launch authority through admission races.',
        'steps': [{**step(), 'hypothesis': {
            'problem': 'Lost admission responses can duplicate work.',
            'audience': 'Operators resuming an exploration.',
            'baseline': 'Manual reconciliation of admitted runs.',
            'scenario': 'Two writers compete to admit a frozen launch.',
            'mechanism': 'Reuse the owner-bound idempotent run record.',
            'expected_value': 'Avoid duplicate dispatch; not yet measured.',
            'falsifier': 'A losing admission dispatches another task.',
            'assumptions': ['The winning admission is durably readable.'],
            'evidence_refs': ['Synthetic admission-conflict injection.']}}, step('final')],
        'budget': {'max_wall_clock_seconds': 60}},
        user_id=owner, idempotency_key='race-binding')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=owner)
    intent = deepcopy(programs.advance_program(pid, user_id=owner)['current'])
    assert intent['step']['hypothesis'] and intent['context_seed']
    assert intent['deadline_at'] == 1060
    original = runs._save
    admissions = []

    def competing_writer(run, version, user_id):
        result = original(run, version, user_id)
        if version == 0:
            admissions.append(run['run_id'])
            # Model the winning writer's committed record before the loser
            # learns that its version-zero admission conflicted.
            raise StorageError('database_conflict', 'Another writer won admission')
        return result

    monkeypatch.setattr(runs, '_save', competing_writer)
    advanced = programs.advance_program(pid, user_id=owner)
    saved = runs.get_run(intent['run_id'], user_id=owner)
    assert admissions == [intent['run_id']]
    assert advanced['current'] == intent
    assert saved['exploration_hypothesis'] == intent['step']['hypothesis']
    assert saved['context_seed'] == intent['context_seed']
    assert saved['absolute_deadline_at'] == intent['deadline_at']
    assert saved['objective'] == intent['payload']['objective']
    assert saved['budget'] == intent['payload']['budget']
    assert saved['status'] == 'preparing' and saved['task_id'] == ''
    assert tasks == []  # Losing admission must not dispatch the winner's work.
    retried = runs.submit_run(intent['payload'], user_id=owner,
        idempotency_key=f'{pid}:experiment:{intent["sequence"]}',
        context_seed=intent['context_seed'], deadline_at=intent['deadline_at'],
        exploration_hypothesis=intent['step']['hypothesis'])
    assert retried == saved and tasks == []


def test_receipt_commit_retries_without_double_accounting(runtime, monkeypatch):
    programs, store, _ = runtime
    program = launch(runtime, create(runtime))
    original = programs._save
    def fail_program_save(value, version, user_id):
        if value['settled_through']:
            raise StorageError('database_unavailable', 'program header not committed')
        return original(value, version, user_id)
    monkeypatch.setattr(programs, '_save', fail_program_save)
    with pytest.raises(StorageError):
        finish(runtime, program)
    assert len([key for key in store.rows if key[0] == programs.EXPERIMENTS]) == 1
    monkeypatch.setattr(programs, '_save', original)
    settled = command(runtime, program, 'advance')
    assert settled['actuals']['tokens'] == 23 and settled['settled_through'] == 1


def test_failed_experiment_requires_explicit_retry_and_preserves_negative_memory(runtime):
    program = launch(runtime, create(runtime))
    failed = finish(runtime, program, 'needs_attention')
    assert failed['status'] == 'needs_attention'
    command(runtime, failed, 'advance')
    with pytest.raises(runtime[0].EvolutionStateError):
        command(runtime, failed, 'resume')
    assert len(runtime[2]) == 1
    command(runtime, failed, 'retry')
    command(runtime, failed, 'advance')
    retried = command(runtime, failed, 'advance')
    assert retried['current']['sequence'] == 2
    assert runtime[2][-1]['run_id'] != runtime[2][0]['run_id']
    assert json.loads(runtime[2][-1]['seed'])['recent_experiments'][0]['outcome'] == 'needs_attention'


def test_expansion_keeps_final_audit_and_current_intent_pinned(runtime):
    programs, _, _ = runtime
    program = launch(runtime, create(runtime))
    expanded = programs.append_steps(program['program_id'], {'steps': [step(title='Repeat on held-out inputs')]}, user_id=OWNER, idempotency_key='expand')
    assert expanded['current'] == program['current']
    assert expanded['queue'][-1]['kind'] == 'final'
    assert len(expanded['queue']) == 3
    with pytest.raises(programs.EvolutionContractError):
        programs.append_steps(program['program_id'], {'steps': [step('final')]}, user_id=OWNER, idempotency_key='bad-expand')


def test_ambiguous_expansion_commit_is_exactly_once_even_after_later_expansions(runtime, monkeypatch):
    programs, _, _ = runtime
    program = create(runtime)
    original = programs._seal_expansion
    def lost_receipt(program, user_id):
        if program.get('last_expansion'):
            raise StorageError('database_timeout', 'expansion receipt response unavailable')
        return original(program, user_id)
    payload = {'steps': [step(title='Check another hypothesis')]}
    monkeypatch.setattr(programs, '_seal_expansion', lost_receipt)
    with pytest.raises(StorageError):
        programs.append_steps(program['program_id'], payload, user_id=OWNER, idempotency_key='append-one')
    assert len(programs.get_program(program['program_id'], user_id=OWNER)['queue']) == 3
    monkeypatch.setattr(programs, '_seal_expansion', original)
    repeated = programs.append_steps(program['program_id'], payload, user_id=OWNER, idempotency_key='append-one')
    assert len(repeated['queue']) == 3
    programs.append_steps(program['program_id'], payload, user_id=OWNER, idempotency_key='append-two')
    repeated = programs.append_steps(program['program_id'], payload, user_id=OWNER, idempotency_key='append-one')
    assert len(repeated['queue']) == 4
    with pytest.raises(programs.EvolutionStateError):
        programs.append_steps(program['program_id'], {'steps': [step(title='Changed')]}, user_id=OWNER, idempotency_key='append-one')


def test_program_limits_apply_across_experiments(runtime):
    program = launch(runtime, create(runtime, budget={'max_tokens': 23}))
    settled = finish(runtime, program)
    exhausted = command(runtime, settled, 'advance')
    assert exhausted['status'] == 'needs_attention' and 'max_tokens' in exhausted['error']
    assert len(runtime[2]) == 1


def test_scheduler_visits_all_enrolled_programs_and_keeps_archive_out(runtime):
    programs, _, launched = runtime
    first = create(runtime, 'first')
    second = create(runtime, 'second')
    command(runtime, first, 'resume')
    command(runtime, second, 'resume')
    for _ in range(4):
        assert len(programs.sweep_programs(user_id=OWNER, batch_size=1)) == 2
    assert len(launched) == 2
    command(runtime, first, 'cancel')
    command(runtime, second, 'cancel')
    programs.sweep_programs(user_id=OWNER)
    assert programs.sweep_programs(user_id=OWNER) == []


@pytest.mark.parametrize('steps', [[step()], [step('final'), step()], [step('final'), step('final')]])
def test_contract_rejects_missing_or_nonterminal_final_audit(runtime, steps):
    with pytest.raises(runtime[0].EvolutionContractError):
        create(runtime, steps=steps)


def test_maximum_objective_is_preserved_and_acceptance_has_separate_space(runtime):
    objective = 'x' * 4000
    program = launch(runtime, create(runtime, objective=objective, steps=[step('final')]))
    assert runtime[2][0]['payload']['objective'] == objective


def test_unknown_requirement_cannot_be_hidden_by_passing_aggregate_review():
    from lib.evolution import runs
    run = {'acceptance_requirements': ['Restart preserves history.']}
    review = {'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [{'criterion_id': key, 'outcome': 'pass',
        'evidence_ref': 'Observed checks'} for key in runs.RUBRIC],
        'requirement_results': [{'requirement_id': 'requirement-1', 'outcome': 'unknown',
                                'evidence_ref': 'No restart was observed.'}]}
    verdict = runs._parse_review({'content': json.dumps(review), '_userId':123}, run=run)
    assert verdict['outcome'] == 'failure'
    assert verdict['criterion_results'][0]['outcome'] == 'unknown'
    del review['requirement_results']
    with pytest.raises(runs.EvolutionContractError):
        runs._parse_review({'content': json.dumps(review)}, run=run)


def test_research_adapter_never_silently_ignores_custom_acceptance_requirements(task_runtime):
    runs, _, tasks = task_runtime
    with pytest.raises(runs.EvolutionContractError, match='custom goal acceptance'):
        runs.submit_run({'objective': 'Research durable execution.', 'domain': 'research',
                         'acceptance_requirements': ['Observe recovery after a real crash.']},
                        user_id=123)
    assert tasks == []


def test_program_executes_and_archives_real_run_review_and_episode(task_runtime):
    from lib.evolution import programs, get_episode
    runs, _, tasks = task_runtime
    owner = 123  # Explicit identity owned by the task-engine test fixture.
    program = programs.create_program({'objective': 'Explain binary search.',
        'acceptance': ['Explain why the interval halves each step.'],
        'steps': [step('final')]}, user_id=owner, idempotency_key='real-program')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=owner)
    programs.advance_program(pid, user_id=owner)
    active = programs.advance_program(pid, user_id=owner)
    assert len(tasks) == 1
    assert 'requirement-1' in tasks[0]['messages'][0]['content']
    reviewing = finish_task(task_runtime, tasks[0], 'Each comparison halves the interval.')
    from tests.evolution.test_evolution_source_tools import read_all
    read_all(tasks[1], reviewing)
    assert 'acceptance_requirements' in tasks[1]['messages'][0]['content']
    review = {'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [{'criterion_id': key, 'outcome': 'pass',
                'evidence_ref': 'The delivered explanation supports this check.'} for key in runs.RUBRIC],
              'requirement_results': [{'requirement_id': f'requirement-{index + 1}', 'outcome': 'pass',
                'evidence_ref': 'Observed in the delivered explanation.',
                'original_sources':[deepcopy(reviewing['result_source'])]} for index in range(2)]}
    finish_task(task_runtime, tasks[1], json.dumps(review))
    completed = programs.advance_program(pid, user_id=owner)
    assert completed['status'] == 'completed' and completed['actuals']['tokens'] == 30
    receipt = programs.experiment_page(pid, user_id=owner)['experiments'][0]
    assert len(receipt['review']['requirement_results']) == 2
    run = runs.get_run(active['current']['run_id'], user_id=owner)
    assert get_episode(run['episode_id'], user_id=owner)['status'] == 'success'


@pytest.mark.parametrize('hypothesis', ['mechanism', 'scenario'])
@pytest.mark.parametrize('outcome', ['unknown', 'fail'])
def test_default_unfalsifiable_hypothesis_blocks_success_and_retains_review(
        task_runtime, hypothesis, outcome):
    from lib.evolution import programs, get_episode
    runs, _, tasks = task_runtime
    owner = 123
    program = programs.create_program({
        'objective': 'Discover real problems and conceive new mechanisms and emerging applications.',
        'auto_replan': False, 'max_attempts': 1,
    }, user_id=owner, idempotency_key='default-hypothesis-review')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=owner)
    for kind in ['discovery', 'baseline', 'experiment']:
        programs.advance_program(pid, user_id=owner)
        active = programs.advance_program(pid, user_id=owner)
        assert active['current']['step']['kind'] == kind
        run = runs.get_run(active['current']['run_id'], user_id=owner)
        checks = run['acceptance_requirements']
        # Synthetic provider verdicts exercise the real task/run/program
        # settlement boundary; they are not evidence that a model can assess value.
        target = None
        if kind == 'experiment':
            target = next((index for index, check in enumerate(checks)
                           if hypothesis in check.lower() and 'falsifier' in check.lower()), None)
            assert target is not None, 'Default independent review must cover falsifiability'
        execution = tasks[-1]
        if kind == 'baseline':
            execution['toolRounds'] = [{'toolName': 'run_command', 'toolCallId': 'baseline',
                'status': 'done', 'results': [{'exitCode': '0'}]}]
        finish_task(task_runtime, execution, 'Synthetic experiment evidence and proposed hypotheses.')
        review = {'evidence_audit': {'complete': True, 'objections': []}, 'criterion_results': [{'criterion_id': key, 'outcome': 'pass',
                  'evidence_ref': 'Synthetic aggregate reviewer verdict.'} for key in runs.RUBRIC],
                  'requirement_results': [{'requirement_id': f'requirement-{index + 1}',
                  'outcome': outcome if index == target else 'pass',
                  'evidence_ref': 'No observable falsifier supplied.' if index == target
                      else 'Synthetic reviewed check.'} for index in range(len(checks))]}
        finish_task(task_runtime, tasks[-1], json.dumps(review))
        settled = programs.advance_program(pid, user_id=owner)
    assert settled['status'] == 'needs_attention'
    assert settled['settled_through'] == 3
    receipt = programs.experiment_page(pid, user_id=owner)['experiments'][-1]
    expected_verdict = 'failure' if outcome == 'unknown' else 'partial'
    assert receipt['review']['outcome'] == expected_verdict
    assert receipt['review']['requirement_results'][target]['outcome'] == outcome
    assert receipt['review']['requirement_results'][target]['evidence_ref'] == 'No observable falsifier supplied.'
    failed = runs.get_run(active['current']['run_id'], user_id=owner)
    assert get_episode(failed['episode_id'], user_id=owner)['status'] == expected_verdict
    assert len(tasks) == 6  # No extra executor or final audit after the failed review.


def test_default_policy_change_preserves_saved_frontier_and_frozen_retry(runtime, monkeypatch):
    programs, _, launched = runtime
    request = {'objective': 'Explore a user problem and its possible solutions.'}
    program = programs.create_program(request, user_id=OWNER, idempotency_key='saved-policy')
    original_queue = deepcopy(program['queue'])
    active = launch(runtime, program)
    frozen = deepcopy(active['current'])
    monkeypatch.setattr(programs, '_default_steps', lambda: [step('final', 'Changed policy')])
    retried = programs.create_program(request, user_id=OWNER, idempotency_key='saved-policy')
    assert retried['queue'] == original_queue
    assert retried['current'] == frozen
    reconciled = command(runtime, retried, 'advance')
    assert reconciled['current'] == frozen and len(launched) == 1
    fresh = programs.create_program(request, user_id=OWNER, idempotency_key='fresh-policy')
    assert fresh['queue'] != original_queue


@pytest.mark.parametrize('boundary', ['problem_selection', 'enabling_change'])
@pytest.mark.parametrize('outcome', ['unknown', 'fail'])
def test_default_value_discovery_cannot_pass_unresolved_selection_or_necessity(
        task_runtime, boundary, outcome):
    from lib.evolution import programs, get_episode
    runs, _, tasks = task_runtime
    owner = 123
    program = programs.create_program({
        'objective': 'Find a valuable problem and explore a future mechanism.',
        'auto_replan': False, 'max_attempts': 1,
    }, user_id=owner, idempotency_key=f'value-discovery-{boundary}-{outcome}')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=owner)
    stages = ['discovery'] if boundary == 'problem_selection' else ['discovery', 'baseline', 'experiment']
    for kind in stages:
        programs.advance_program(pid, user_id=owner)
        active = programs.advance_program(pid, user_id=owner)
        assert active['current']['step']['kind'] == kind
        run = runs.get_run(active['current']['run_id'], user_id=owner)
        checks = run['acceptance_requirements']
        target = None
        if kind == stages[-1]:
            terms = ('alternative explanations', 'problem') if boundary == 'problem_selection' else ('enabling change', 'necessity')
            target = next((index for index, check in enumerate(checks)
                           if all(term in check.lower() for term in terms)), None)
            assert target is not None, 'Value discovery requires an independently reviewed discriminating check'
        if kind == 'baseline':
            tasks[-1]['toolRounds'] = [{'toolName': 'run_command', 'toolCallId': 'baseline',
                'status': 'done', 'results': [{'exitCode': '0'}]}]
        finish_task(task_runtime, tasks[-1], 'Synthetic evidence packet; no claim of measured demand or new technology.')
        judgment = {'evidence_audit': {'complete': True, 'objections': []},
            'criterion_results': [{'criterion_id': key, 'outcome': 'pass',
                'evidence_ref': 'Synthetic provider verdict.'} for key in runs.RUBRIC],
            'requirement_results': [{'requirement_id': f'requirement-{index + 1}',
                'outcome': outcome if index == target else 'pass',
                'evidence_ref': 'Alternative explanation or present workaround was not examined.'
                    if index == target else 'Synthetic reviewed check.'} for index in range(len(checks))]}
        finish_task(task_runtime, tasks[-1], json.dumps(judgment))
        settled = programs.advance_program(pid, user_id=owner)
    assert settled['status'] == 'needs_attention' and settled['settled_through'] == len(stages)
    receipt = programs.experiment_page(pid, user_id=owner)['experiments'][-1]
    assert receipt['review']['requirement_results'][target]['outcome'] == outcome
    assert receipt['review']['requirement_results'][target]['evidence_ref'] == 'Alternative explanation or present workaround was not examined.'
    episode = get_episode(run['episode_id'], user_id=owner)
    assert episode['status'] != 'success' and len(tasks) == 2 * len(stages)


def test_conditional_future_proposal_retains_unknown_value_after_protocol_acceptance(task_runtime):
    from lib.evolution import programs, exploration_tools
    from lib.evolution.hypothesis_review import AXES, digest
    runs, _, tasks = task_runtime
    owner = 123
    program = programs.create_program({
        'objective': 'Explore a future workflow under explicit enabling conditions.',
        'steps': [step('discovery'), step('final', 'Audit original objective')],
        'auto_replan': False, 'max_attempts': 1,
    }, user_id=owner, idempotency_key='conditional-future-proposal')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=owner)
    programs.advance_program(pid, user_id=owner)
    active = programs.advance_program(pid, user_id=owner)
    hypothesis = {
        'problem': 'Handoffs may duplicate a remote action after a lost response.',
        'audience': 'An operator resuming a remote investigation.',
        'baseline': 'Present operator reconciliation using the remote action log.',
        'scenario': 'Conditional unattended continuation after lost responses.',
        'mechanism': 'The remote service would provide a verifiable action outcome proof.',
        'expected_value': 'Potential reduction of reconciliation effort; unmeasured.',
        'falsifier': 'Present logs match the proof method at equal information and operating cost.',
        'assumptions': ['Service proofs are not yet available in this setup.', 'Observed ambiguity does not prove adoption demand.'],
        'evidence_refs': ['Synthetic lost-response incident; not evidence of market demand.'],
    }
    proposed = {**step(title='Specify a conditional outcome-proof protocol'), 'hypothesis': hypothesis}
    exploration_tools.execute(tasks[-1], {'program_id': pid, 'action': 'propose', 'step': proposed},
                              call_id='conditional-future')
    queued = programs.get_program(pid, user_id=owner)['queue'][-2]
    assert queued['hypothesis'] == hypothesis and queued['checks'][0] == proposed['checks'][0]
    assert any('alternative explanations' in check for check in queued['checks'])
    assert any('enabling change' in check and 'necessity' in check for check in queued['checks'])

    for sequence in (1, 2):
        if sequence == 2:
            programs.advance_program(pid, user_id=owner)
            active = programs.advance_program(pid, user_id=owner)
            assert active['current']['step']['hypothesis'] == hypothesis
        run = runs.get_run(active['current']['run_id'], user_id=owner)
        finish_task(task_runtime, tasks[-1], 'Conditional protocol delivered; missing prerequisite and present-log control retained. No measured result.')
        judgment = {'evidence_audit': {'complete': True, 'objections': []},
            'criterion_results': [{'criterion_id': key, 'outcome': 'pass',
                'evidence_ref': 'Synthetic protocol acceptance, not technology efficacy.'} for key in runs.RUBRIC],
            'requirement_results': [{'requirement_id': f'requirement-{index + 1}', 'outcome': 'pass',
                'evidence_ref': 'Conditional protocol states comparisons, unresolved prerequisites and no value claim.'}
                for index in range(len(run['acceptance_requirements']))]}
        if sequence == 2:
            judgment['hypothesis_assessment'] = {'hypothesis_sha256': digest(hypothesis), **{
                axis: {'outcome': 'unknown', 'evidence_ref': 'Prerequisite and demand evidence unavailable; protocol only.'}
                for axis in AXES}}
        finish_task(task_runtime, tasks[-1], json.dumps(judgment))
        settled = programs.advance_program(pid, user_id=owner)
    assert settled['status'] == 'active' and settled['settled_through'] == 2
    assert settled['queue'][-1]['kind'] == 'final'
    receipt = programs.experiment_page(pid, user_id=owner)['experiments'][-1]
    assert receipt['step']['hypothesis'] == hypothesis and receipt['review']['outcome'] == 'success'
    assert all(receipt['review']['hypothesis_assessment'][axis]['outcome'] == 'unknown' for axis in AXES)
    assert len(tasks) == 4


def test_cancel_racing_submission_is_observed_and_cancels_the_created_run(runtime, monkeypatch):
    programs, _, launched = runtime
    program = create(runtime)
    command(runtime, program, 'resume')
    command(runtime, program, 'advance')
    original = programs.runs.submit_run
    def racing(*args, **kwargs):
        command(runtime, program, 'cancel')
        return original(*args, **kwargs)
    monkeypatch.setattr(programs.runs, 'submit_run', racing)
    cancelled = command(runtime, program, 'advance')
    assert cancelled['status'] == 'cancelled' and len(launched) == 1
    assert programs.runs.get_run(launched[0]['run_id'], user_id=OWNER)['status'] == 'cancelled'


def test_wall_deadline_survives_waiting_between_launch_intent_and_real_submission(task_runtime, monkeypatch):
    from lib.evolution import programs
    _, _, tasks = task_runtime
    clock = [1000]
    monkeypatch.setattr(programs.time, 'time', lambda: clock[0])
    program = programs.create_program({'objective': 'Explain binary search.',
        'budget': {'max_wall_clock_seconds': 10}, 'steps': [step('final')]},
        user_id=123, idempotency_key='delayed-program')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=123)
    intent = programs.advance_program(pid, user_id=123)
    assert intent['current']['deadline_at'] == 1010
    clock[0] = 1020
    programs.advance_program(pid, user_id=123)
    assert tasks == []
    settled = programs.advance_program(pid, user_id=123)
    assert settled['status'] == 'needs_attention'
    assert 'budget_exhausted' in settled['error']


def test_long_history_has_constant_read_cost_and_no_durable_receipt_eviction(runtime, monkeypatch):
    programs, store, _ = runtime
    program = launch(runtime, create(runtime))
    settled = finish(runtime, program)
    receipt = programs.experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]
    for sequence in range(2, 401):
        archived = {**receipt, 'sequence': sequence, 'result': 'Complete durable output ' * 1000}
        store.rows[(programs.EXPERIMENTS,
                    programs._experiment_key(program['program_id'], sequence, OWNER))] = archived
    header, version = programs._read(program['program_id'], OWNER)
    header['settled_through'] = 400
    programs._save(header, version, OWNER)
    query = store.query
    reads = []
    def measured(operation, payload):
        reads.append((operation, payload.get('namespace')))
        return query(operation, payload)
    monkeypatch.setattr(store, 'query', measured)
    intent = programs.advance_program(program['program_id'], user_id=OWNER)['current']
    assert len([row for row in reads if row[1] == programs.EXPERIMENTS]) == programs.MEMORY_WINDOW
    memory = json.loads(intent['context_seed'])
    assert memory['older_experiments_omitted'] == 394
    assert all(item['preview_truncated'] for item in memory['recent_experiments'])
    first_page = programs.experiment_page(program['program_id'], user_id=OWNER, limit=1)
    assert first_page['experiments'][0]['sequence'] == 1
    assert len([key for key in store.rows if key[0] == programs.EXPERIMENTS]) == 400


def test_start_submits_first_intent_immediately_and_is_idempotent(runtime):
    program = create(runtime)
    started = command(runtime, program, 'start')
    assert started['current'] and len(runtime[2]) == 1
    command(runtime, started, 'start')
    assert len(runtime[2]) == 1
    command(runtime, started, 'pause')
    command(runtime, started, 'start')
    assert len(runtime[2]) == 1


def test_project_directory_is_canonical_and_pinned_across_experiments(runtime, tmp_path):
    project = tmp_path / 'repo'
    project.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(project, target_is_directory=True)
    program = create(runtime, project_path=str(alias))
    assert program['execution_options']['project_path'] == str(project)
    program = command(runtime, program, 'start')
    assert runtime[2][0]['payload']['project_path'] == str(project)
    settled = finish(runtime, program)
    command(runtime, settled, 'advance')
    command(runtime, settled, 'advance')
    assert runtime[2][1]['payload']['project_path'] == str(project)
    with pytest.raises(runtime[0].EvolutionStateError):
        create(runtime, project_path=str(tmp_path))


def test_invalid_project_is_rejected_before_persisting_or_launching(runtime, tmp_path):
    with pytest.raises(runtime[0].EvolutionContractError):
        create(runtime, project_path=str(tmp_path / 'missing'))
    assert not runtime[0].list_programs(user_id=OWNER) and not runtime[2]


def test_catalog_keeps_large_intents_and_frontiers_out_of_polling_payload(runtime):
    program = command(runtime, create(runtime, objective='x' * 4000), 'start')
    catalog = runtime[0].list_programs(user_id=OWNER)
    assert len(catalog) == 1 and len(catalog[0]['objective']) == 180
    assert catalog[0]['current_title'] == program['current']['step']['title']
    assert catalog[0]['pending_steps'] == 2
    assert set(catalog[0]) == {'program_id', 'objective', 'status', 'settled_through',
                             'pending_steps', 'current_title', 'created_at', 'updated_at'}
    assert len(json.dumps(catalog)) < 800


@pytest.mark.parametrize('attempts', [None, 0, 7])
def test_repair_authorization_preserves_explicit_limits_and_freezes_default_replan_window(runtime, attempts):
    options = {} if attempts is None else {'max_attempts': attempts}
    program = command(runtime, create(runtime, **options), 'start')
    payload = program['current']['payload']
    if attempts is None:
        assert payload['max_attempts'] == 3
    else:
        assert payload['max_attempts'] == attempts
    # Progression never re-resolves a frozen launch against a new default.
    frozen = deepcopy(program['current'])
    command(runtime, program, 'pause'); command(runtime, program, 'resume')
    assert runtime[0].get_program(program['program_id'], user_id=OWNER)['current'] == frozen


def test_finite_unknown_consumption_cannot_authorize_fresh_retry(runtime):
    programs, store, launched = runtime
    program = launch(runtime, create(runtime, budget={'max_tokens': 100}))
    rid = program['current']['run_id']
    run = programs.runs.get_run(rid, user_id=OWNER)
    run['actuals_complete'] = False
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(rid, OWNER, kind='run'), run, kind='run')
    program = finish(runtime, program, 'failed')
    assert program['actuals_complete'] is False
    before = deepcopy(program)
    writes = store.writes
    with pytest.raises(programs.EvolutionStateError, match='program token consumption is unknown'):
        command(runtime, program, 'retry')
    assert programs.get_program(program['program_id'], user_id=OWNER) == before
    assert store.writes == writes
    assert len(launched) == 1


@pytest.mark.parametrize('action,status', [('start', 'ready'), ('resume', 'paused'), ('retry', 'needs_attention')])
@pytest.mark.parametrize('dimension', ['max_tokens', 'max_tool_calls', 'max_wall_clock_seconds', 'unknown_tokens'])
def test_blocked_program_commands_preserve_header_and_enrollment(runtime, monkeypatch, action, status, dimension):
    programs, store, launched = runtime
    budget = {dimension: 10} if dimension != 'unknown_tokens' else {'max_tokens': 100}
    program = create(runtime, budget=budget)
    program.update(status=status)
    if dimension == 'unknown_tokens':
        program['actuals_complete'] = False
    elif dimension == 'max_wall_clock_seconds':
        if action == 'start':
            program.pop('started_at')  # Legacy preparation already spends from creation.
        else:
            program['started_at'] = program['created_at']
        monkeypatch.setattr(programs.time, 'time', lambda: program['created_at'] + 10)
    else:
        program['actuals'][dimension.removeprefix('max_')] = 10
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    writes = store.writes
    monkeypatch.setattr(programs, '_enroll', lambda *args: pytest.fail('Blocked command must not enroll'))
    with pytest.raises(programs.EvolutionStateError, match='program (limit reached|token consumption is unknown)'):
        command(runtime, program, action)
    assert programs.get_program(program['program_id'], user_id=OWNER) == program
    assert store.writes == writes and launched == []


@pytest.mark.parametrize('budget', [None, {'max_tokens': 0}, {'max_tool_calls': 10}])
def test_unknown_consumption_without_token_cap_retains_lower_bound_and_allows_explicit_retry(runtime, budget):
    programs, _, launched = runtime
    program = launch(runtime, create(runtime, budget=budget))
    rid = program['current']['run_id']
    run = programs.runs.get_run(rid, user_id=OWNER)
    run['actuals_complete'] = False
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(rid, OWNER, kind='run'), run, kind='run')
    program = finish(runtime, program, 'failed')
    receipt = deepcopy(programs.experiment_page(program['program_id'], user_id=OWNER))
    program = command(runtime, program, 'retry')
    program = command(runtime, program, 'advance')
    program = command(runtime, program, 'advance')
    assert len(launched) == 2 and program['actuals_complete'] is False
    assert program['tool_calls_complete'] is True
    assert program['actuals']['tokens'] == 23
    assert programs.experiment_page(program['program_id'], user_id=OWNER) == receipt


def test_reaper_checks_unknown_usage_and_cancel_remains_available(runtime):
    programs, _, launched = runtime
    program = create(runtime, budget={'max_tokens': 100})
    program.update(status='active', actuals_complete=False)
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    program = command(runtime, program, 'advance')
    assert program['status'] == 'needs_attention' and program['current'] is None
    assert program['error'] == 'program token consumption is unknown' and launched == []
    program = command(runtime, program, 'cancel')
    assert program['status'] == 'cancelled' and program['actuals_complete'] is False


def test_resume_preserves_existing_frozen_intent_even_when_header_usage_is_unknown(runtime):
    programs, _, launched = runtime
    program = launch(runtime, create(runtime, budget={'max_tokens': 100}))
    frozen = deepcopy(program['current'])
    program = command(runtime, program, 'pause')
    program['actuals_complete'] = False
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    program = command(runtime, program, 'resume')
    assert program['status'] == 'active' and program['current'] == frozen
    assert program['actuals_complete'] is False and len(launched) == 1


def test_current_context_reads_are_exact_bounded_and_digest_bound_across_pause(runtime):
    from lib.evolution.contracts import EvolutionContractError, EvolutionStateError
    program = launch(runtime, create(runtime, objective='原始问题'+'范围'*1900,
        acceptance=['代价与收益'+'条件'*250 for _ in range(16)]))
    task = context_executor(runtime, program); writes = runtime[1].writes
    first = read_context(task, program)
    assert len(first['content']) == 6000 and first['next_offset'] == 6000
    command(runtime, program, 'pause');writes = runtime[1].writes
    page = read_context(task, program, offset=6000, source_digest=first['source_digest'])
    assert page['source_digest'] == first['source_digest'] and page['offset'] == 6000
    with pytest.raises(EvolutionContractError):
        read_context(task, program, offset=6000)
    with pytest.raises(EvolutionStateError):
        read_context(task, program, source_digest='0'*64)
    with pytest.raises(EvolutionContractError):
        read_context(task, program, offset=first['total_chars']+1, source_digest=first['source_digest'])
    assert runtime[1].writes == writes
    for mutation in ('foreign_owner', 'stale_task', 'reviewer'):
        from copy import deepcopy
        other = deepcopy(task)
        if mutation == 'foreign_owner':other['_userId'] += 1
        if mutation == 'stale_task':other['id'] = 'stale'
        if mutation == 'reviewer':other['config']['evolution']['stage'] = 'reviewing'
        with pytest.raises(EvolutionStateError):read_context(other, program)


@pytest.mark.parametrize('action,status', [('start', 'ready'), ('resume', 'paused'), ('retry', 'needs_attention')])
@pytest.mark.parametrize('dimension', ['tokens', 'tool_calls', 'wall_clock_seconds'])
@pytest.mark.parametrize('excess', [0, 1])
def test_review_reserve_blocks_fresh_commands_before_enrollment(runtime, monkeypatch, action, status, dimension, excess):
    programs, store, launched = runtime
    program = create(runtime, budget={'max_'+dimension: 100}, review_reserve={dimension: 20})
    program['status'] = status
    if dimension == 'wall_clock_seconds':
        if action == 'start':
            program.pop('started_at')  # Preserve admission checks for legacy records.
        else:
            program['started_at'] = program['created_at']
        monkeypatch.setattr(programs.time, 'time', lambda: program['created_at'] + 80 + excess)
    else:
        program['actuals'][dimension] = 80 + excess
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    writes = store.writes
    monkeypatch.setattr(programs, '_enroll', lambda *args: pytest.fail('Unavailable review reserve must refuse before enrollment'))
    with pytest.raises(programs.EvolutionStateError, match='program review reserve unavailable'):
        command(runtime, program, action)
    assert programs.get_program(program['program_id'], user_id=OWNER) == program
    assert store.writes == writes and launched == []


@pytest.mark.parametrize('dimension', ['tokens', 'tool_calls', 'wall_clock_seconds'])
def test_review_reserve_preserves_one_unit_of_execution_and_original_intent(runtime, monkeypatch, dimension):
    programs, store, launched = runtime
    program = create(runtime, budget={'max_'+dimension: 100}, review_reserve={dimension: 20})
    if dimension == 'wall_clock_seconds':
        program['started_at'] = program['created_at']
        monkeypatch.setattr(programs.time, 'time', lambda: program['created_at'] + 79)
    else:
        program['actuals'][dimension] = 79
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    program = command(runtime, program, 'resume')
    program = command(runtime, program, 'advance')
    assert program['current']['payload']['budget']['max_'+dimension] == 21
    assert program['current']['payload']['review_reserve'][dimension] == 20
    frozen = deepcopy(program['current'])
    program['status'] = 'paused'
    if dimension == 'wall_clock_seconds':
        monkeypatch.setattr(programs.time, 'time', lambda: program['created_at'] + 80)
    else:
        program['actuals'][dimension] = 80
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    resumed = command(runtime, program, 'resume')
    assert resumed['status'] == 'active' and resumed['current'] == frozen
    assert launched == []


def test_legacy_incomplete_tool_allowance_requires_inspection_before_retry(runtime):
    programs, store, launched = runtime
    program = create(runtime, budget={'max_tokens': 0, 'max_tool_calls': 100})
    program.update(status='needs_attention', actuals_complete=False)
    program.pop('tool_calls_complete', None)
    programs.records.put_record(programs.PROGRAMS,
        programs.records.entity_key(program['program_id'], OWNER, kind='program'), program, kind='program')
    before = deepcopy(program); writes = store.writes
    with pytest.raises(programs.EvolutionStateError, match='program tool consumption is unknown'):
        command(runtime, program, 'retry')
    assert programs.get_program(program['program_id'], user_id=OWNER) == before
    assert store.writes == writes and launched == []


@pytest.mark.parametrize('action', ['start', 'resume'])
def test_prepared_plan_clock_starts_only_on_explicit_activation(runtime, monkeypatch, action):
    programs, _, launched = runtime
    clock = [1000]
    monkeypatch.setattr(programs.time, 'time', lambda: clock[0])
    program = create(runtime, budget={'max_wall_clock_seconds': 100})
    assert program['started_at'] is None and program['status'] == 'ready'
    clock[0] = 5000
    programs.sweep_programs(user_id=OWNER)
    assert not launched and programs._remaining_budget(program)['max_wall_clock_seconds'] == 100
    program = command(runtime, program, action)
    assert program['started_at'] == 5000
    if not program['current']:
        program = command(runtime, program, 'advance')
    assert program['current']['deadline_at'] == 5100
    command(runtime, program, 'pause')
    clock[0] = 5040
    resumed = command(runtime, program, 'resume')
    assert resumed['started_at'] == 5000
    assert resumed['current'] == program['current']
    assert programs._remaining_budget(resumed)['max_wall_clock_seconds'] == 60


def test_checkpoint_settlement_survives_restart_without_launching_successor(runtime, monkeypatch):
    programs, _, launched = runtime
    program = launch(runtime, create(runtime, advance_mode='checkpoint'))
    settled = finish(runtime, program)
    assert settled['status'] == 'paused' and settled['current'] is None
    assert settled['settled_through'] == 1 and len(launched) == 1
    assert programs.experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]['result']
    programs.sweep_programs(user_id=OWNER)
    assert len(launched) == 1
    assert programs.get_program(program['program_id'], user_id=OWNER)['status'] == 'paused'
    resumed = command(runtime, settled, 'resume')
    resumed = command(runtime, resumed, 'advance')
    resumed = command(runtime, resumed, 'advance')
    assert resumed['current']['sequence'] == 2 and len(launched) == 2
    complete = finish(runtime, resumed)
    assert complete['status'] == 'completed' and complete['settled_through'] == 2


def test_pacing_change_does_not_launch_resume_or_modify_frozen_intent(runtime):
    programs, _, launched = runtime
    program = create(runtime)
    def pacing(mode):
        return programs.command_program(program['program_id'], {'action': 'set_pacing', 'advance_mode': mode}, user_id=OWNER)
    prepared = pacing('checkpoint')
    assert prepared['status'] == 'ready' and prepared['started_at'] is None and not launched
    active = launch(runtime, prepared)
    current = deepcopy(active['current'])
    assert pacing('continuous')['current'] == current
    assert pacing('checkpoint')['current'] == current and len(launched) == 1
    paused = finish(runtime, active)
    assert paused['status'] == 'paused'
    changed = pacing('continuous')
    assert changed['status'] == 'paused' and not changed['current']
    programs.sweep_programs(user_id=OWNER)
    assert len(launched) == 1


def test_checkpoint_pauses_before_automatic_failure_diagnosis(runtime):
    from tests.evolution.test_evolution_replanning import fail_review
    program = launch(runtime, create(runtime, advance_mode='checkpoint', auto_replan=True))
    settled = fail_review(runtime, program)
    assert settled['status'] == 'paused' and settled['queue'][0]['kind'] == 'replan'
    runtime[0].sweep_programs(user_id=OWNER)
    assert len(runtime[2]) == 1
    assert runtime[0].experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]['outcome'] == 'needs_attention'


def test_checkpoint_receipt_commit_failure_reconciles_once_and_stays_paused(runtime, monkeypatch):
    programs, _, launched = runtime
    program = launch(runtime, create(runtime, advance_mode='checkpoint'))
    original = programs._save
    def lose_header(value, version, user_id):
        if value['settled_through']:
            raise StorageError('database_timeout', 'header not committed')
        return original(value, version, user_id)
    monkeypatch.setattr(programs, '_save', lose_header)
    with pytest.raises(StorageError):
        finish(runtime, program)
    monkeypatch.setattr(programs, '_save', original)
    settled = command(runtime, program, 'advance')
    assert settled['status'] == 'paused' and settled['actuals']['tokens'] == 23
    assert settled['settled_through'] == 1 and len(launched) == 1


def test_legacy_program_retains_creation_clock_and_default_continuous_pacing(runtime, monkeypatch):
    programs, _, _ = runtime
    clock = [1000]
    monkeypatch.setattr(programs.time, 'time', lambda: clock[0])
    program = create(runtime, budget={'max_wall_clock_seconds': 100})
    legacy, version = programs._read(program['program_id'], OWNER)
    legacy.pop('started_at'); legacy.pop('advance_mode')
    programs._save(legacy, version, OWNER)
    clock[0] = 1020
    assert programs._remaining_budget(legacy)['max_wall_clock_seconds'] == 80
    settled = finish(runtime, launch(runtime, legacy))
    assert settled['status'] == 'active'
    assert 'started_at' not in settled and 'advance_mode' not in settled


@pytest.mark.parametrize('payload', [
    {'action': 'set_pacing'}, {'action': 'set_pacing', 'advance_mode': 'unlimited'},
    {'action': 'resume', 'advance_mode': 'checkpoint'},
])
def test_invalid_pacing_command_fails_before_mutation(runtime, payload):
    programs, store, launched = runtime
    program = create(runtime)
    writes = store.writes
    with pytest.raises(programs.EvolutionContractError):
        programs.command_program(program['program_id'], payload, user_id=OWNER)
    assert store.writes == writes and not launched


@pytest.mark.parametrize('boundary', ['active', 'paused', 'needs_attention', 'completed', 'cancelled'])
def test_initial_start_retry_cannot_grant_successor_permission(runtime, boundary):
    programs, store, launched = runtime
    program = launch(runtime, create(runtime, advance_mode='checkpoint', auto_replan=False))
    if boundary == 'paused':
        program = finish(runtime, program)
    elif boundary == 'needs_attention':
        program = finish(runtime, program, 'failed')
    elif boundary == 'completed':
        program = finish(runtime, program)
        program = command(runtime, program, 'resume')
        program = command(runtime, program, 'advance')
        program = command(runtime, program, 'advance')
        program = finish(runtime, program)
    elif boundary == 'cancelled':
        program = command(runtime, program, 'cancel')
    assert program['status'] == boundary
    before = deepcopy(program)
    writes, launches = store.writes, len(launched)
    assert command(runtime, program, 'start') == before
    assert store.writes == writes and len(launched) == launches
    if boundary == 'paused':
        programs.sweep_programs(user_id=OWNER)
        assert len(launched) == launches
        assert programs.get_program(program['program_id'], user_id=OWNER) == before


def test_stale_initial_start_read_cannot_resume_settled_checkpoint(runtime):
    program = create(runtime, advance_mode='checkpoint')
    stale_prepared = deepcopy(program)
    settled = finish(runtime, launch(runtime, program))
    assert stale_prepared['started_at'] is None and settled['status'] == 'paused'
    assert command(runtime, stale_prepared, 'start') == settled
    assert len(runtime[2]) == 1


@pytest.mark.parametrize('outcome', ['unknown', 'fail'])
@pytest.mark.parametrize('missing_requirement', ['Fresh reproduction', 'Execution provenance', 'Validation feedback exposure'])
def test_missing_validation_evidence_blocks_final_audit_and_retains_evidence(task_runtime, outcome, missing_requirement):
    from lib.evolution import programs, get_episode
    runs, _, tasks = task_runtime
    owner=123
    program=programs.create_program({'objective':'Reproduce a held-out improvement.',
        'auto_replan':False,'max_attempts':1},user_id=owner,idempotency_key='missing-reproduction-'+outcome)
    pid=program['program_id']
    programs.command_program(pid,{'action':'resume'},user_id=owner)
    for index in range(5):
        programs.advance_program(pid,user_id=owner)
        active=programs.advance_program(pid,user_id=owner)
        run=runs.get_run(active['current']['run_id'],user_id=owner)
        checks=run['acceptance_requirements']
        from tests.evolution.test_evolution_scientific_observations import protocol
        value=protocol()
        from tests.support.research_observations import synthetic_frozen_scope
        value['measurement_scope'] = synthetic_frozen_scope(value)
        if index==3:
            prereg=run['validation_preregistration_target']
            programs.preregister_validation(pid,prereg['target_step_sha256'],value,'supported',user_id=owner,executor_run_id=run['run_id'])
        target=None
        if index==4:
            target=next(i for i,check in enumerate(checks) if missing_requirement in check)
        if index == 1:
            tasks[-1]['toolRounds'] = [{'toolName': 'run_command', 'toolCallId': 'baseline',
                'status': 'done', 'results': [{'exitCode': '0'}]}]
        if index == 4:
            value = run['scientific_protocol']
            tasks[-1]['toolRounds'] = [{'toolName':'run_command','toolCallId':'setup','status':'done','results':[{'exitCode':'0'}]}]
            from tests.support.research_observations import observation_payload
            from lib.project_mod.run_command import _format_run_output
            measured = observation_payload(value, 'metrics', 'supported')
            comparison = measured['scientific_observations']['comparison']
            comparison['method']['system_digest'] = value['measurement_scope']['candidate_digest']
            comparison['baseline']['system_digest'] = value['measurement_scope']['baseline_digest']
            tasks[-1]['messages'].append({'role':'tool','tool_call_id':'setup','content':_format_run_output('cat old_measurements.json',json.dumps(measured),'',0)})
        finish_task(task_runtime,tasks[-1],'Archived candidate/control report; no fresh experiment was executed.')
        if index==4:
            from tests.evolution.test_evolution_source_tools import delivered
            current=runs.get_run(run['run_id'],user_id=owner)
            assert current['status'] == 'reviewing'  # Numeric consistency passes; fresh reproduction still does not.
            ref=current['tool_evidence_source']
            delivered(tasks[-1],{'action':'read','run_id':run['run_id'],'task_id':ref['task_id'],'source_kind':'tool_evidence','source_sha256':ref['source_sha256'],'offset':0})
        judgment={'evidence_audit':{'complete':True,'objections':[]},
            'criterion_results':[{'criterion_id':key,'outcome':'pass','evidence_ref':'Synthetic aggregate verdict.'} for key in runs.RUBRIC],
            'requirement_results':[{'requirement_id':f'requirement-{i+1}',
                'outcome':outcome if i==target else 'pass',
                'evidence_ref':f'{missing_requirement} is missing; only self-reported input identities exist.' if i==target else 'Synthetic reviewed evidence.'}
                for i in range(len(checks))]}
        finish_task(task_runtime,tasks[-1],json.dumps(judgment))
        settled=programs.advance_program(pid,user_id=owner)
    assert settled['status']=='needs_attention'
    assert settled['settled_through']==5
    assert settled['current'] is None and settled['queue'][-1]['kind']=='final'
    receipt=programs.experiment_page(pid,user_id=owner)['experiments'][-1]
    assert receipt['review']['requirement_results'][target]['outcome']==outcome
    assert f'{missing_requirement} is missing' in receipt['review']['requirement_results'][target]['evidence_ref']
    assert get_episode(run['episode_id'],user_id=owner)['status']!='success'
    assert len(tasks)==10


@pytest.mark.parametrize('boundary', ['active', 'paused', 'needs_attention', 'completed', 'cancelled'])
def test_legacy_checkpoint_start_retry_cannot_authorize_successor(runtime, monkeypatch, boundary):
    programs, store, launched = runtime
    clock = [1000]
    monkeypatch.setattr(programs.time, 'time', lambda: clock[0])
    program = create(runtime, auto_replan=False, budget={'max_wall_clock_seconds': 100})
    legacy, version = programs._read(program['program_id'], OWNER)
    legacy.pop('started_at'); legacy.pop('advance_mode')
    programs._save(legacy, version, OWNER)
    program = programs.command_program(program['program_id'],
        {'action': 'set_pacing', 'advance_mode': 'checkpoint'}, user_id=OWNER)
    assert program['status'] == 'ready' and 'started_at' not in program and not launched
    clock[0] = 1020
    program = command(runtime, program, 'start')
    frozen = deepcopy(program['current'])
    assert frozen['deadline_at'] == 1100 and len(launched) == 1
    if boundary == 'paused':
        program = finish(runtime, program)
    elif boundary == 'needs_attention':
        program = finish(runtime, program, 'failed')
    elif boundary == 'completed':
        program = finish(runtime, program)
        program = command(runtime, program, 'resume')
        program = command(runtime, program, 'advance')
        program = command(runtime, program, 'advance')
        program = finish(runtime, program)
    elif boundary == 'cancelled':
        program = command(runtime, program, 'cancel')
    assert program['status'] == boundary and 'started_at' not in program
    before = deepcopy(program); writes, launches = store.writes, len(launched)
    clock[0] = 1040
    assert command(runtime, legacy, 'start') == before
    assert store.writes == writes and len(launched) == launches
    assert programs._remaining_budget(before)['max_wall_clock_seconds'] == 60
    if boundary == 'active':
        assert before['current'] == frozen
    if boundary == 'paused':
        # Switching back to continuous pacing cannot restore the stale grant.
        before = programs.command_program(program['program_id'],
            {'action': 'set_pacing', 'advance_mode': 'continuous'}, user_id=OWNER)
        assert command(runtime, legacy, 'start') == before
        programs.sweep_programs(user_id=OWNER)
        assert len(launched) == launches
        resumed = command(runtime, before, 'resume')
        resumed = command(runtime, resumed, 'advance')
        resumed = command(runtime, resumed, 'advance')
        assert resumed['current']['sequence'] == 2 and len(launched) == launches + 1
        assert resumed['current']['deadline_at'] == frozen['deadline_at']


def test_legacy_pause_without_activation_witness_requires_explicit_resume(runtime):
    programs, store, launched = runtime
    program = create(runtime)
    legacy, version = programs._read(program['program_id'], OWNER)
    legacy.pop('started_at')
    programs._save(legacy, version, OWNER)
    paused = command(runtime, legacy, 'pause')
    writes = store.writes
    assert command(runtime, legacy, 'start') == paused
    assert store.writes == writes and not launched
    resumed = command(runtime, paused, 'resume')
    resumed = command(runtime, resumed, 'advance')
    resumed = command(runtime, resumed, 'advance')
    assert len(launched) == 1 and resumed['current']['sequence'] == 1
    assert 'started_at' not in resumed


def test_lazy_history_freezes_its_frontier_and_refuses_wrong_receipt_identity(runtime):
    programs,store,_=runtime
    program=finish(runtime,launch(runtime,create(runtime)),result='original')
    iterator=programs.iter_experiments(program['program_id'],user_id=OWNER,limit=50)
    current,version=programs._read(program['program_id'],OWNER)
    current['settled_through']=2;programs._save(current,version,OWNER)
    assert next(iterator)['sequence']==1
    with pytest.raises(StopIteration):next(iterator)
    key=(programs.EXPERIMENTS,programs._experiment_key(program['program_id'],1,OWNER))
    store.rows[key]['sequence']=2
    with pytest.raises(programs.EvolutionStateError,match='identity changed'):
        next(programs.iter_experiments(program['program_id'],user_id=OWNER,limit=1))


def test_final_original_policy_is_frozen_for_new_programs_and_legacy_retries(runtime, monkeypatch):
    programs, _, launched = runtime
    from lib.evolution import review_evidence
    monkeypatch.setattr(review_evidence,'VERSION',2)
    legacy = create(runtime,request_id='legacy-final-policy',steps=[step('final')])
    assert 'final_evidence_version' not in legacy
    monkeypatch.setattr(review_evidence,'VERSION',3)
    repeated = create(runtime,request_id='legacy-final-policy',steps=[step('final')])
    assert repeated == legacy
    launch(runtime,legacy)
    assert 'require_requirement_originals' not in launched[-1]['payload']
    fresh = create(runtime,request_id='new-final-policy',steps=[step('final')])
    assert fresh['final_evidence_version'] == 1
    launch(runtime,fresh)
    assert launched[-1]['payload']['require_requirement_originals'] is True


def test_required_original_policy_cannot_be_enabled_without_explicit_requirements(task_runtime):
    runs, _, tasks = task_runtime
    with pytest.raises(runs.EvolutionContractError,match='explicit acceptance requirements'):
        runs.submit_run({'objective':'Audit original evidence.','require_requirement_originals':True},user_id=123)
    assert not tasks


@pytest.mark.parametrize('changed', ['objective', 'requirements', 'policy', 'intent_policy'])
def test_new_final_program_never_archives_completion_under_changed_acceptance(runtime, changed):
    programs, _, launched = runtime
    program = launch(runtime,create(runtime,request_id='final-bind-'+changed,steps=[step('final')]))
    run = programs.runs.get_run(program['current']['run_id'],user_id=OWNER)
    if changed == 'objective': run['objective'] = 'A smaller substitute objective.'
    elif changed == 'requirements': run['acceptance_requirements'] = ['One easier check.']
    elif changed == 'policy': run.pop('require_requirement_originals')
    else:
        stored,version = programs._read(program['program_id'],OWNER)
        stored['current']['payload'].pop('require_requirement_originals')
        programs._save(stored,version,OWNER)
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(run['run_id'],OWNER,kind='run'),run,kind='run')
    with pytest.raises(programs.EvolutionStateError,match='final acceptance binding changed'):
        finish(runtime,program)
    current = programs.get_program(program['program_id'],user_id=OWNER)
    assert current['status'] == 'active' and current['settled_through'] == 0
    assert current['current']['run_id'] == run['run_id']
    assert programs.experiment_page(program['program_id'],user_id=OWNER)['experiments'] == []
    assert len(launched) == 1


@pytest.mark.parametrize('changed', ['none','run_format','run_rule','run_outcome','run_execution','requirements','intent_protocol'])
def test_identified_validation_archive_requires_original_launch_binding(runtime, changed):
    programs, _, launched = runtime
    from tests.evolution.test_evolution_scientific_observations import protocol
    from tests.support.research_observations import synthetic_frozen_scope
    value = protocol(); value['measurement_scope'] = synthetic_frozen_scope(value)
    value['observation_format'] = 'tofu.scientific-observations/v2'
    validation = deepcopy(programs._default_steps()[-2])
    validation.update(scientific_protocol=value, scientific_required_outcome='supported')
    program = launch(runtime, create(runtime, steps=[validation,step('final')], request_id='validation-binding-'+changed))
    run = programs.runs.get_run(program['current']['run_id'], user_id=OWNER)
    if changed == 'run_format': run['scientific_protocol'].pop('observation_format')
    elif changed == 'run_rule': run['scientific_protocol']['decision_rule']['minimum_effect'] = 999
    elif changed == 'run_outcome': run['scientific_required_outcome'] = 'refuted'
    elif changed == 'run_execution': run.pop('require_command_execution')
    elif changed == 'requirements': run['acceptance_requirements'] = ['One easier check.']
    elif changed == 'intent_protocol':
        stored, version = programs._read(program['program_id'], OWNER)
        stored['current']['payload']['scientific_protocol'].pop('observation_format')
        programs._save(stored, version, OWNER)
    programs.records.put_record(programs.records.NAMESPACE_RUNS,
        programs.records.entity_key(run['run_id'], OWNER, kind='run'), run, kind='run')
    if changed == 'none':
        settled = finish(runtime, program)
        assert settled['settled_through'] == 1
    else:
        with pytest.raises(programs.EvolutionStateError, match='validation acceptance binding changed'):
            finish(runtime, program)
        current = programs.get_program(program['program_id'], user_id=OWNER)
        assert current['settled_through'] == 0 and current['current']['run_id'] == run['run_id']
        assert programs.experiment_page(program['program_id'], user_id=OWNER)['experiments'] == []
    assert len(launched) == 1


@pytest.mark.parametrize('lost_ack', [False, True])
def test_programme_state_publication_distinguishes_missing_write_from_lost_ack(runtime, monkeypatch, lost_ack):
    programs, store, launched = runtime
    program = create(runtime)
    command(runtime, program, 'resume')
    before = programs.get_program(program['program_id'], user_id=OWNER)
    original = programs.records.put_record
    def failed(namespace, key, value, **kwargs):
        if namespace == programs.PROGRAMS:
            if lost_ack:
                original(namespace, key, value, **kwargs)
                raise StorageError('database_unavailable', 'lost programme ACK')
            return kwargs['expected_version'] + 1
        return original(namespace, key, value, **kwargs)
    monkeypatch.setattr(programs.records, 'put_record', failed)
    if lost_ack:
        published = command(runtime, before, 'advance')
        assert published['current']
        assert programs.get_program(program['program_id'], user_id=OWNER) == published
    else:
        with pytest.raises(StorageError, match='exactly acknowledged'):
            command(runtime, before, 'advance')
        assert programs.get_program(program['program_id'], user_id=OWNER) == before
    assert launched == []


def test_confirmed_programme_intent_survives_selected_authority_restart(storage, monkeypatch):
    from lib.evolution import programs, records
    supervisor, _ = storage
    monkeypatch.setattr(records, '_storage', lambda **kwargs: supervisor.client)
    program = programs.create_program({'objective':'Preserve this mission and its budget',
        'budget':{'max_tokens':10000}, 'steps':[{'kind':'final','title':'Original objective audit',
            'instruction':'Audit the original objective from observed evidence.', 'checks':['Observed evidence required.']}]},
        user_id=1, idempotency_key='confirmed-native-intent')
    programs.command_program(program['program_id'], {'action':'resume'}, user_id=1)
    committed = programs.advance_program(program['program_id'], user_id=1)
    assert committed['current'] and committed['actuals']['tokens'] == 0
    supervisor.stop(); supervisor.start()
    restored = programs.get_program(program['program_id'], user_id=1)
    assert restored == committed
    assert restored['current']['payload']['budget']['max_tokens'] == 10000
    assert restored['objective'] == 'Preserve this mission and its budget'


def test_complete_frontier_and_expansion_cross_old_thirty_two_step_ceiling(runtime):
    programs, _, launched = runtime
    original_steps = [step(title=f'original-{index}') for index in range(65)] + [step('final')]
    program = launch(runtime, create(runtime, 'complete-frontier', steps=original_steps))
    additions = [step(title=f'addition-{index}') for index in range(65)]
    expanded = programs.append_steps(program['program_id'], {'steps': additions},
                                     user_id=OWNER, idempotency_key='large-expansion')
    assert expanded['queue'] == original_steps[:-1] + additions + original_steps[-1:]
    assert expanded['current'] == program['current']
    assert expanded['objective'] == program['objective'] and expanded['acceptance'] == program['acceptance']
    assert len(launched) == 1
    repeated = programs.append_steps(program['program_id'], {'steps': additions},
                                     user_id=OWNER, idempotency_key='large-expansion')
    assert repeated == expanded
    with pytest.raises(programs.EvolutionStateError):
        programs.append_steps(program['program_id'], {'steps': additions},
                              user_id=OWNER + 1, idempotency_key='foreign')


def test_registry_enrolls_beyond_old_program_count_without_losing_prior_ids(runtime):
    programs, store, launched = runtime
    key = programs.records.entity_key('programs', OWNER, kind='program registry')
    ids = [f'program-{index:032x}' for index in range(4096)]
    programs.records.put_record(programs.REGISTRY, key, {'program_ids': ids, 'cursor': 4095},
                                 kind='program registry', expected_version=0)
    import time
    import tracemalloc
    tracemalloc.start(); started = time.perf_counter()
    program = create(runtime, 'program-after-4096')
    program = command(runtime, program, 'resume')
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
    registry = programs.records.get_record(programs.REGISTRY, key)
    assert registry['program_ids'] == ids + [program['program_id']]
    assert registry['cursor'] == 4095 and not launched
    assert programs.records.get_record(programs.REGISTRY,
        programs.records.entity_key('programs', OWNER + 1, kind='program registry')) is None
    print(f'program_registry_ids={len(registry["program_ids"])} '
          f'json_bytes={len(json.dumps(registry).encode())} enroll_seconds={elapsed:.4f} traced_peak_bytes={peak}')


def _registered_sweep_fixture(runtime, count):
    programs, store, _ = runtime
    ids = [f'program-{index:032x}' for index in range(count)]
    key = programs.records.entity_key('programs', OWNER, kind='program registry')
    programs.records.put_record(programs.REGISTRY, key, {'program_ids': ids, 'cursor': 0},
                                 kind='program registry', expected_version=0)
    return ids, key


def test_sweep_starts_all_independent_programs_with_idle_hint_one_and_joins(runtime, monkeypatch):
    import threading
    import time
    programs, store, _ = runtime
    ids, _ = _registered_sweep_fixture(runtime, 64)
    barrier = threading.Barrier(len(ids))
    seen = []
    def advance(program_id, *, user_id):
        assert user_id == OWNER
        seen.append(program_id)
        barrier.wait(timeout=10)
        return {'status': 'active', 'current': {'run_id': program_id}}
    monkeypatch.setattr(programs, 'advance_program', advance)
    writes = store.writes
    started = time.perf_counter()
    outcomes = programs.sweep_programs(user_id=OWNER, batch_size=1)
    elapsed = time.perf_counter() - started
    assert sorted(seen) == ids
    assert [row['program_id'] for row in outcomes] == ids
    assert all(row['status'] == 'active' for row in outcomes)
    assert store.writes == writes
    assert not any(thread.name.startswith('tofu-evolution-program') for thread in threading.enumerate())
    print(f'program_sweep_rendezvous={len(ids)} idle_hint=1 seconds={elapsed:.4f} workers_after=0')


def test_failed_worker_start_does_not_enter_program_or_suppress_sibling(runtime, monkeypatch):
    import threading
    programs, _, _ = runtime
    ids, key = _registered_sweep_fixture(runtime, 2)
    original = threading.Thread.start
    failed = []
    def start(thread):
        if thread.name.startswith('tofu-evolution-program') and not failed:
            failed.append(thread.name)
            raise RuntimeError('synthetic worker start failure')
        return original(thread)
    monkeypatch.setattr(threading.Thread, 'start', start)
    seen = []
    monkeypatch.setattr(programs, 'advance_program', lambda program_id, **kwargs:
        seen.append(program_id) or {'status': 'active', 'current': {'run_id': program_id}})
    outcomes = programs.sweep_programs(user_id=OWNER, batch_size=1)
    assert outcomes[0]['status'] == 'deferred' and outcomes[1]['status'] == 'active'
    assert seen == ids[1:]
    assert programs.records.get_record(programs.REGISTRY, key)['program_ids'] == ids
    assert not any(thread.name.startswith('tofu-evolution-program') for thread in threading.enumerate())


@pytest.mark.parametrize('concurrent_change', ['new_enrollment', 'resume'])
def test_sweep_cleanup_batches_terminal_ids_and_preserves_concurrent_enrollment(runtime, monkeypatch, concurrent_change):
    programs, store, _ = runtime
    ids, key = _registered_sweep_fixture(runtime, 40)
    monkeypatch.setattr(programs, 'advance_program', lambda *args, **kwargs:
                        {'status': 'completed', 'current': None})
    before = store.writes
    assert len(programs.sweep_programs(user_id=OWNER)) == 40
    assert programs.records.get_record(programs.REGISTRY, key)['program_ids'] == []
    assert store.writes == before + 1
    programs.records.put_record(programs.REGISTRY, key, {'program_ids': ids, 'cursor': 0},
                                 kind='program registry')
    original = programs.records.put_record
    def interleave(namespace, entity, value, **kwargs):
        if namespace == programs.REGISTRY and not value['program_ids']:
            current, version = programs.records.get_versioned_record(namespace, entity)
            if concurrent_change == 'new_enrollment':
                current['program_ids'].append('program-new')
            original(namespace, entity, current, kind='program registry', expected_version=version)
        return original(namespace, entity, value, **kwargs)
    monkeypatch.setattr(programs.records, 'put_record', interleave)
    programs.sweep_programs(user_id=OWNER)
    assert programs.records.get_record(programs.REGISTRY, key)['program_ids'] == ids + (['program-new'] if concurrent_change == 'new_enrollment' else [])


def test_blocked_program_does_not_hold_independent_visits(runtime, monkeypatch):
    import threading
    programs, _, _ = runtime
    ids, _ = _registered_sweep_fixture(runtime, 8)
    release = threading.Event()
    healthy = threading.Event()
    seen = []
    def advance(program_id, **kwargs):
        if program_id == ids[0]:
            assert release.wait(timeout=10)
        else:
            seen.append(program_id)
            if len(seen) == len(ids) - 1:
                healthy.set()
        return {'status': 'active', 'current': {'run_id': program_id}}
    monkeypatch.setattr(programs, 'advance_program', advance)
    completed = []
    caller = threading.Thread(target=lambda: completed.append(
        programs.sweep_programs(user_id=OWNER, batch_size=1)), name='program-sweep-test-caller')
    caller.start()
    try:
        assert healthy.wait(timeout=5)
        assert not completed and caller.is_alive()
    finally:
        release.set()
        caller.join(timeout=10)
    assert not caller.is_alive() and len(completed[0]) == len(ids)
    assert not any(thread.name.startswith('tofu-evolution-program') for thread in threading.enumerate())

def test_final_stage_freezes_its_procedure_without_rewriting_the_original_goal(runtime):
    programs, _, launched = runtime
    original = 'z' * 4000
    procedure = 'python3 inspect_frozen_experiment.py --owner 512'
    final = dict(step('final', 'Audit exact originals'), instruction=procedure)
    program = create(runtime, objective=original, steps=[final])
    program = launch(runtime, program)
    payload = launched[-1]['payload']
    assert payload['objective'] == original
    assert payload['execution_instruction'] == procedure
    assert payload['acceptance_requirements'] == program['acceptance'] + final['checks']
    frozen = deepcopy(program['current']['payload'])
    program = command(runtime, program, 'advance')
    assert program['current']['payload'] == frozen


@pytest.mark.parametrize('schema_budget', [0, 4000])
def test_executor_plugins_and_schema_policy_survive_program_launch_and_final_stage(runtime, schema_budget):
    program = create(runtime, executor_plugins=['environments'], executor_schema_budget_tokens=schema_budget)
    assert program['execution_options']['executor_plugins'] == ['environments']
    program = launch(runtime, program)
    assert runtime[2][0]['payload']['executor_plugins'] == ['environments']
    assert runtime[2][0]['payload']['executor_schema_budget_tokens'] == schema_budget
    program = finish(runtime, program)
    for _ in range(3):
        program = command(runtime, program, 'advance')
        if len(runtime[2]) == 2:
            break
    assert len(runtime[2]) == 2
    assert runtime[2][1]['payload']['executor_plugins'] == ['environments']
    assert runtime[2][1]['payload']['executor_schema_budget_tokens'] == schema_budget
    with pytest.raises(runtime[0].EvolutionStateError):
        create(runtime, executor_plugins=[])

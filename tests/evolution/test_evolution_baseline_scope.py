"""Saved experiment protocols remain authoritative when defaults change."""
from copy import deepcopy
import pytest
from tests.evolution.test_evolution_programs import (
    runtime as program_runtime, OWNER, launch, finish, step,
)

from tests.evolution.test_evolution_runs import runtime as _execution_runtime_fixture, finish as finish_execution, OWNER as EXECUTION_OWNER
execution_runtime = _execution_runtime_fixture

pytestmark = pytest.mark.unit
runtime = program_runtime


def test_saved_baseline_protocol_survives_later_default_changes(runtime, monkeypatch):
    programs, _, launched = runtime
    original = programs.create_program({'objective': 'Qualify observations before comparing mechanisms.',
        'acceptance': ['Every population remains identified.']}, user_id=OWNER,
        idempotency_key='saved-qualified-baseline')
    saved_baseline = deepcopy(original['queue'][1])
    monkeypatch.setattr(programs, '_default_steps', lambda: [
        step('baseline', 'Changed defaults'), step('final', 'Changed final')])
    discovery = launch(runtime, original)
    ready = finish(runtime, discovery)
    baseline = launch(runtime, ready)
    assert baseline['current']['step'] == saved_baseline
    assert saved_baseline['require_command_execution'] is True
    assert launched[-1]['payload']['require_command_execution'] is True
    assert launched[-1]['payload']['acceptance_requirements'] == saved_baseline['checks']
    assert saved_baseline['instruction'] in launched[-1]['payload']['objective']
    assert baseline['objective'] == original['objective']
    assert baseline['acceptance'] == original['acceptance']


def test_custom_baseline_checks_are_not_replaced_by_new_defaults(runtime):
    programs, _, launched = runtime
    custom = step('baseline', 'Read the independently frozen protocol')
    custom['checks'] = ['Use only the original external population and denominator.']
    program = programs.create_program({'objective': 'Run the custom frozen comparison.',
        'steps': [custom, step('final', 'Original final audit')]}, user_id=OWNER,
        idempotency_key='custom-qualified-baseline')
    current = launch(runtime, program)
    assert current['current']['step'] == custom
    assert 'require_command_execution' not in launched[-1]['payload']
    assert launched[-1]['payload']['acceptance_requirements'] == custom['checks']


def test_default_baseline_refuses_prose_without_execution_receipt(execution_runtime):
    from lib.evolution.programs import _default_steps
    runs, _, tasks = execution_runtime
    baseline = next(row for row in _default_steps() if row['kind'] == 'baseline')
    run = runs.submit_run({'objective': baseline['instruction'],
        'acceptance_requirements': baseline['checks'],
        'require_command_execution': baseline['require_command_execution'],
        'max_attempts': 1}, user_id=EXECUTION_OWNER)
    result = finish_execution(execution_runtime, tasks[0],
        'The baseline passed every test and achieved 1000 requests per second.')
    assert result['status'] != 'completed'
    assert result['error'] == 'required_command_execution_unobserved'
    assert result['review'] is None
    assert result['result'] == 'The baseline passed every test and achieved 1000 requests per second.'
    assert len(tasks) == 1
    assert runs.get_run(run['run_id'], user_id=EXECUTION_OWNER)['error'] == result['error']


@pytest.mark.parametrize('length', [70, 24001, 60000])
@pytest.mark.parametrize('auto_replan', [False, True])
def test_refused_baseline_archives_original_and_keeps_final_queued(execution_runtime, length, auto_replan):
    from lib.evolution import programs
    from lib.evolution.receipt_sources import read_original_page
    _, _, tasks = execution_runtime
    baseline = next(row for row in programs._default_steps() if row['kind'] == 'baseline')
    program = programs.create_program({'objective': 'Measure the current throughput.',
        'steps': [baseline, step('final', 'Audit')], 'auto_replan': auto_replan, 'max_attempts': 1},
        user_id=EXECUTION_OWNER, idempotency_key='refused-baseline')
    active = programs.command_program(program['program_id'], {'action': 'start'}, user_id=EXECUTION_OWNER)
    text = ('Unexecuted measurement claim. ' * (length // 28 + 1))[:length]
    finish_execution(execution_runtime, tasks[-1], text)
    settled = programs.advance_program(program['program_id'], user_id=EXECUTION_OWNER)
    assert settled['status'] == 'needs_attention' and settled['settled_through'] == 1
    assert settled['queue'][-1]['kind'] == 'final' and len(tasks) == 1
    assert settled['queue'][0] == baseline
    for _ in range(3):
        unchanged = programs.advance_program(program['program_id'], user_id=EXECUTION_OWNER)
        assert unchanged == settled and len(tasks) == 1
    receipt = programs.experiment_page(program['program_id'], user_id=EXECUTION_OWNER)['experiments'][0]
    assert receipt['error'] == 'required_command_execution_unobserved'
    assert receipt['result'] == text[:24000] and receipt['result_source']['total_chars'] == len(text)
    chunks = []
    offset = 0
    while offset < len(text):
        page = read_original_page(receipt, 'deliverable', user_id=EXECUTION_OWNER, offset=offset)
        chunks.append(page['content'])
        offset += len(page['content'])
    assert ''.join(chunks) == text


@pytest.mark.parametrize('changed', [False, True])
def test_committed_original_receipt_recovers_header_without_reinspection(execution_runtime, monkeypatch, changed):
    from lib.evolution import programs, receipt_sources
    from lib.storage.errors import StorageError
    runs, store, tasks = execution_runtime
    baseline = next(row for row in programs._default_steps() if row['kind'] == 'baseline')
    program = programs.create_program({'objective': 'Measure throughput.',
        'steps': [baseline, step('final', 'Audit')], 'auto_replan': False, 'max_attempts': 1},
        user_id=EXECUTION_OWNER, idempotency_key='archive-header-loss')
    active = programs.command_program(program['program_id'], {'action': 'start'}, user_id=EXECUTION_OWNER)
    failed = finish_execution(execution_runtime, tasks[-1], 'Unexecuted claim. ' * 3000)
    save = programs._save
    def unavailable(value, version, user_id):
        if value['settled_through']:
            raise StorageError('database_unavailable', 'header commit unavailable')
        return save(value, version, user_id)
    monkeypatch.setattr(programs, '_save', unavailable)
    with pytest.raises(StorageError):
        programs.advance_program(program['program_id'], user_id=EXECUTION_OWNER)
    archive_key = programs._experiment_key(program['program_id'], 1, EXECUTION_OWNER)
    before = programs.records.get_record(programs.EXPERIMENTS, archive_key)
    assert before is not None
    monkeypatch.setattr(programs, '_save', save)
    def forbid_reinspection(*args, **kwargs):
        pytest.fail('Already committed receipt must not re-read its original')
    monkeypatch.setattr(receipt_sources, 'read_original_page', forbid_reinspection)
    if changed:
        failed['result_source']['source_sha256'] = '0' * 64
        programs.records.put_record(programs.records.NAMESPACE_RUNS,
            programs.records.entity_key(failed['run_id'], EXECUTION_OWNER, kind='run'), failed, kind='run')
        with pytest.raises(programs.EvolutionStateError, match='immutable'):
            programs.advance_program(program['program_id'], user_id=EXECUTION_OWNER)
        assert programs.get_program(program['program_id'], user_id=EXECUTION_OWNER)['current'] == active['current']
    else:
        settled = programs.advance_program(program['program_id'], user_id=EXECUTION_OWNER)
        assert settled['settled_through'] == 1 and settled['status'] == 'needs_attention'
        assert settled['actuals'] == failed['actuals'] and len(tasks) == 1
    assert programs.records.get_record(programs.EXPERIMENTS, archive_key) == before

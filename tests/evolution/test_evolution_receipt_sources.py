"""Short originals keep immutable provenance without changing review policy."""
from copy import deepcopy
import hashlib
import json

import pytest

from lib.evolution import semantic_memory as memory
from tests.evolution.test_evolution_semantic_memory import archived as archived, runtime as runtime
from tests.evolution.test_evolution_exploration_tools import call
from tests.evolution.test_evolution_runs import runtime as _run_runtime_fixture, finish
run_runtime = _run_runtime_fixture
from tests.evolution.test_evolution_programs import OWNER

pytestmark = pytest.mark.unit


@pytest.fixture
def short_original(runtime, archived):
    from lib.evolution import stage_outcomes
    from lib.evolution.source_tools import reference
    program, task, attempts = archived
    receipt = runtime[0].experiment_page(program['program_id'], user_id=OWNER)['experiments'][0]
    run = runtime[0].runs.get_run(receipt['run_id'], user_id=OWNER)
    run.update(task_id='short-original', episode_id='short-episode', attempt=1, receipts=[])
    runtime[0].records.put_record(runtime[0].records.NAMESPACE_RUNS,
        runtime[0].records.entity_key(run['run_id'], OWNER, kind='run'), run, kind='run')
    content = '前段未验证观察 🧪\n' * 750 + 'TAIL: network saturated'
    terminal = {'id': run['task_id'], '_userId': OWNER, 'status': 'done',
        'finishReason': 'stop', 'content': content, 'created_at': 1, 'finished_at': 2,
        'usage': {'input_tokens': 10, 'output_tokens': 5},
        'config': {'evolution': {'run_id': run['run_id'], 'stage': 'executing'}},
        'messages': [{'role': 'tool', 'tool_call_id': 'observation', 'content': 'Observed source'}]}
    metadata = stage_outcomes.capture(run, terminal, user_id=OWNER,
                                      review_prompt_factory=lambda _: 'Frozen review')
    receipt.update(result=content, tool_evidence_source=reference(metadata, 'tool_evidence'))
    receipt.pop('result_source', None)
    runtime[0].records.put_record(runtime[0].EXPERIMENTS,
        runtime[0]._experiment_key(program['program_id'], 1, OWNER), receipt, kind='experiment')
    return program, task, attempts, receipt, content, metadata


def test_legacy_short_original_is_read_from_sealed_pages_without_rewriting_receipt(runtime, short_original):
    program, _, _, receipt, content, metadata = short_original
    before = deepcopy(receipt)
    chunks, cursor, scanned = memory.window(program, {'source_kind': 'deliverable'}, OWNER)
    assert ''.join(row['text'] for row in chunks) == content
    assert cursor is None and scanned == 1
    assert chunks[-1]['source_ref'] == {'task_id': metadata['task_id'],
        'source_sha256': hashlib.sha256(content.encode()).hexdigest(), 'total_chars': len(content)}
    assert runtime[0].experiment_page(program['program_id'], user_id=OWNER)['experiments'][0] == before


def test_legacy_short_original_relevance_and_cache_keep_quote_provenance_and_cost(runtime, short_original):
    program, task, attempts, receipt, content, _ = short_original
    first = call(task, program, 'semantic_recall', query='带宽瓶颈', source_kind='deliverable')
    hit = first['experiments'][0]
    assert first['sources_unavailable'] == 0 and first['source_chars'] == len(content)
    assert hit['excerpt'] == 'network saturated' and hit['source_kind'] == 'deliverable'
    assert hit['excerpt'] in content[hit['offset']:hit['end']]
    assert hit['source_digest'] == hashlib.sha256(content.encode()).hexdigest()
    assert hit['detail_required'] and 'unverified' in first['evidence_status']
    assert call(task, program, 'semantic_recall', query='带宽瓶颈', source_kind='deliverable')['cache_hit']
    assert attempts == ['带宽瓶颈'] and task['_executionBudget'].consumption()[0] == 31
    assert runtime[0].experiment_page(program['program_id'], user_id=OWNER)['experiments'][0] == receipt


@pytest.mark.parametrize('mutation', ['inline_digest', 'inline_length', 'aux_digest', 'aux_length',
    'aux_kind', 'missing_task', 'unsealed', 'reviewer_stage', 'conflicting_tasks', 'damaged_part'])
def test_legacy_short_provenance_refuses_changes_before_any_ranking(runtime, short_original, mutation):
    from lib.evolution import stage_outcomes
    program, task, attempts, receipt, content, metadata = short_original
    if mutation == 'inline_digest': receipt['result'] = 'X' + content[1:]
    elif mutation == 'inline_length': receipt['result'] = content[:-1]
    elif mutation == 'aux_digest': receipt['tool_evidence_source']['source_sha256'] = '0' * 64
    elif mutation == 'aux_length': receipt['tool_evidence_source']['total_chars'] += 1
    elif mutation == 'aux_kind': receipt['tool_evidence_source']['source_kind'] = 'artifact_evidence'
    elif mutation == 'missing_task': receipt['tool_evidence_source']['task_id'] = 'absent-task'
    elif mutation == 'conflicting_tasks':
        receipt['artifact_evidence_source'] = {'task_id': 'another-task', 'source_kind': 'artifact_evidence',
            'source_sha256': '1' * 64, 'total_chars': 1, 'complete': True}
    elif mutation in ('unsealed', 'reviewer_stage'):
        if mutation == 'unsealed': metadata.update(archive_state='collecting', revision=1)
        else: metadata.update(stage='reviewing', review_prompt=None)
        runtime[0].records.put_record(stage_outcomes.NAMESPACE,
            stage_outcomes._key(metadata['task_id'], OWNER), metadata, kind='outcome')
    else:
        key = stage_outcomes._part_key(metadata['task_id'], 0, OWNER)
        part = runtime[0].records.get_record(stage_outcomes.PART_NAMESPACE, key)
        part['text'] = 'X' + part['text'][1:]
        runtime[0].records.put_record(stage_outcomes.PART_NAMESPACE, key, part, kind='part')
    runtime[0].records.put_record(runtime[0].EXPERIMENTS,
        runtime[0]._experiment_key(program['program_id'], 1, OWNER), receipt, kind='experiment')
    with pytest.raises(memory.EvolutionStateError):
        call(task, program, 'semantic_recall', query='带宽瓶颈', source_kind='deliverable')
    assert attempts == [] and '_executionBudget' not in task


@pytest.mark.parametrize('content', ['', '界' * 24000], ids=['empty', 'inline-boundary'])
def test_legacy_exact_empty_and_inline_boundary_originals_keep_source_coverage(runtime, short_original, content):
    from lib.evolution import stage_outcomes
    from lib.evolution.source_tools import reference
    program, _, _, receipt, _, _ = short_original
    run = runtime[0].runs.get_run(receipt['run_id'], user_id=OWNER)
    run['task_id'] = 'boundary-original'
    runtime[0].records.put_record(runtime[0].records.NAMESPACE_RUNS,
        runtime[0].records.entity_key(run['run_id'], OWNER, kind='run'), run, kind='run')
    terminal = {'id': run['task_id'], '_userId': OWNER, 'status': 'error', 'finishReason': 'error',
        'content': content, 'created_at': 1, 'finished_at': 2,
        'usage': {'input_tokens': 10, 'output_tokens': 5},
        'config': {'evolution': {'run_id': run['run_id'], 'stage': 'executing'}},
        'messages': [{'role': 'tool', 'tool_call_id': 'boundary', 'content': 'Observed source'}]}
    metadata = stage_outcomes.capture(run, terminal, user_id=OWNER, review_prompt_factory=lambda _: 'Frozen')
    receipt.update(result=content, tool_evidence_source=reference(metadata, 'tool_evidence'))
    runtime[0].records.put_record(runtime[0].EXPERIMENTS,
        runtime[0]._experiment_key(program['program_id'], 1, OWNER), receipt, kind='experiment')
    chunks, cursor, scanned, unavailable = memory._source_window(program, {'source_kind': 'deliverable'}, OWNER)
    assert ''.join(row['text'] for row in chunks) == content
    assert cursor is None and scanned == 1 and unavailable == 0


@pytest.mark.parametrize('auxiliary', ['none', 'tool', 'artifact'])
@pytest.mark.parametrize('precommitted', ['none', 'old', 'new'])
def test_new_program_indexes_short_executor_after_failed_review_without_changing_review_policy(
        run_runtime, tmp_path, auxiliary, precommitted):
    from lib.evolution import programs, artifact_evidence, stage_outcomes
    from tests.evolution.test_evolution_runs import OWNER as owner
    runs, _, tasks = run_runtime
    step = {'kind': 'discovery', 'title': 'Inspect the source', 'instruction': 'Read it.',
            'checks': ['Evidence is read.']}
    program = programs.create_program({'objective': 'Keep short originals available.',
        'steps': [step, {**step, 'kind': 'final', 'title': 'Audit the objective'}],
        'auto_replan': False, 'max_attempts': 1, 'max_review_format_retries': 0,
        **({'project_path': str(tmp_path)} if auxiliary == 'artifact' else {})},
        user_id=owner, idempotency_key='short-index-' + auxiliary)
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=owner)
    programs.advance_program(pid, user_id=owner)
    active = programs.advance_program(pid, user_id=owner)
    if auxiliary == 'tool':
        tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'long-source',
                                    'content': '工具原始观察\n' * 5000})
    if auxiliary == 'artifact':
        (tmp_path / 'evidence.txt').write_text('Exact frozen source 🧪')
        artifact_evidence.declare(tasks[0], {'path': 'evidence.txt'})
    content = 'Short complete delivery 🧪'
    reviewing = finish(run_runtime, tasks[0], content)
    assert reviewing['status'] == 'reviewing' and 'result_source' not in reviewing
    failed = finish(run_runtime, tasks[1], 'Unparseable review')
    assert failed['status'] == 'failed' and failed['review'] is None
    expected_source = {'task_id': tasks[0]['id'],
        'source_sha256': hashlib.sha256(content.encode()).hexdigest(), 'total_chars': len(content)}
    if precommitted != 'none':
        # Simulate an older or current receipt commit followed by process loss
        # before the program header CAS. Resume must keep those exact bytes.
        current = active['current']
        prior = {'schema_version': 1, 'program_id': pid, 'sequence': current['sequence'],
            'step': current['step'], 'run_id': failed['run_id'], 'outcome': failed['status'],
            'result': failed['result'], 'review': failed['review'], 'actuals': failed['actuals'],
            'error': failed['error'], 'created_at': failed['updated_at']}
        for field in ['tool_evidence_source', 'artifact_evidence_source']:
            if failed.get(field): prior[field] = deepcopy(failed[field])
        if precommitted == 'new': prior['result_source'] = expected_source
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(pid, current['sequence'], owner), prior,
            kind='experiment', expected_version=0)
    settled = programs.advance_program(pid, user_id=owner)
    receipt = programs.experiment_page(pid, user_id=owner)['experiments'][0]
    if precommitted == 'old': assert 'result_source' not in receipt
    else: assert receipt['result_source'] == expected_source
    if precommitted != 'none': assert receipt == prior
    assert receipt['review'] is None and receipt['outcome'] == 'failed'
    assert runs.get_run(active['current']['run_id'], user_id=owner) == failed
    assert len(tasks) == 2 and settled['status'] == 'needs_attention'
    frozen = deepcopy(receipt)
    if auxiliary != 'none':
        receipt.pop('result_source', None)
        programs.records.put_record(programs.EXPERIMENTS,
            programs._experiment_key(pid, 1, owner), receipt, kind='experiment')
        chunks, _, _ = memory.window(settled, {'source_kind': 'deliverable'}, owner)
        assert ''.join(row['text'] for row in chunks) == content
        assert programs.experiment_page(pid, user_id=owner)['experiments'][0] == receipt
    else:
        assert stage_outcomes.page(receipt['run_id'], tasks[0]['id'], user_id=owner)['content'] == content
        assert programs.advance_program(pid, user_id=owner) == settled
        assert programs.experiment_page(pid, user_id=owner)['experiments'][0] == frozen

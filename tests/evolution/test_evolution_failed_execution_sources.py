"""Failed execution evidence stays readable without authorizing success or replay."""
import hashlib
import json

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, finish

pytestmark = pytest.mark.unit


def original(run, source, kind):
    from lib.evolution.stage_outcomes import page
    offset = 0
    chunks = []
    while True:
        row = page(run['run_id'], source['task_id'], user_id=OWNER,
                   source_kind=kind, offset=offset)
        chunks.append(row['content'])
        if row['next_offset'] is None:
            break
        offset = row['next_offset']
    text = ''.join(chunks)
    assert len(text) == source['total_chars']
    assert hashlib.sha256(text.encode()).hexdigest() == source['source_sha256']
    return text


@pytest.mark.parametrize('status,fields', [
    ('error', {'error': 'provider closed prematurely'}),
    ('aborted', {}),
    ('interrupted', {}),
    ('done', {'_executionLimitReason': 'execution_token_usage_unknown'}),
])
@pytest.mark.parametrize('partial', ['', 'Unreviewed result 🧪\n' * 2000], ids=['empty', 'large-unicode'])
def test_failed_executor_retains_exact_output_tool_and_artifact_sources(
        runtime, tmp_path, status, fields, partial):
    from lib.evolution import artifact_evidence
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Inspect a failed experiment.',
        'project_path': str(tmp_path), 'max_attempts': 1}, user_id=OWNER)
    task = tasks[0]
    artifact_text = 'Preserved experiment input 🧪'
    path = tmp_path / 'evidence.txt'
    path.write_text(artifact_text)
    artifact_evidence.declare(task, {'path': 'evidence.txt'})
    tool_text = 'Observed failed operation; this is evidence, not delivery.'
    task['messages'].append({'role': 'tool', 'tool_call_id': 'failed-tool',
                             'content': tool_text})
    task.update(fields)
    failed = finish(runtime, task, partial, status=status)
    assert failed['status'] in {'failed', 'needs_attention'}
    assert failed['review'] is None and len(tasks) == 1
    assert failed['result'] == partial[:24000]
    assert original(failed, failed['result_source'], 'deliverable') == partial
    assert tool_text in original(failed, failed['tool_evidence_source'], 'tool_evidence')
    path.write_text('Later live file replacement')
    rows = [json.loads(line) for line in original(
        failed, failed['artifact_evidence_source'], 'artifact_evidence').splitlines()]
    assert rows[0]['content'] == artifact_text
    assert rows[0]['content_sha256'] == hashlib.sha256(artifact_text.encode()).hexdigest()
    assert runs.advance_run(run['run_id'], user_id=OWNER) == failed
    assert len(tasks) == 1


def test_unknown_usage_stays_unknown_with_readable_failed_sources(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Keep failed source with unknown usage.',
        'budget': {'max_tokens': 1000}, 'max_attempts': 1}, user_id=OWNER)
    task = tasks[0]
    task['messages'].append({'role': 'tool', 'tool_call_id': 'partial-observation',
                             'content': 'Observed result before the unmetered request'})
    task.update(status='error', finishReason='error', content='', usage={},
                _executionLimitReason='execution_token_usage_unknown',
                _executionActualsComplete=False, finished_at=task['created_at'] + 1)
    failed = runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task)
    assert failed['status'] == 'needs_attention'
    assert failed['actuals_complete'] is False
    assert failed['error'] == 'execution_token_usage_unknown'
    assert original(failed, failed['result_source'], 'deliverable') == ''
    assert 'Observed result' in original(failed, failed['tool_evidence_source'], 'tool_evidence')
    assert 'artifact_evidence_source' not in failed
    assert len(tasks) == 1


def test_cancelled_executor_retains_original_without_successor(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Stop with readable partial evidence.'}, user_id=OWNER)
    runs.cancel_run(run['run_id'], user_id=OWNER)
    cancelled = finish(runtime, tasks[0], 'Stopped partial output', status='aborted')
    assert cancelled['status'] == 'cancelled' and cancelled['review'] is None
    assert original(cancelled, cancelled['result_source'], 'deliverable') == 'Stopped partial output'
    assert 'tool_evidence_source' not in cancelled
    assert len(tasks) == 1


def test_failed_reviewer_keeps_executor_sources(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Retain the exact reviewed executor.',
                            'max_attempts': 1}, user_id=OWNER)
    tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'large-tool',
                                 'content': 'Untrusted measurement 🧪\n' * 2000})
    reviewing = finish(runtime, tasks[0], 'Executor original 🧪\n' * 2000)
    executor_sources = {k: reviewing[k] for k in ['result_source', 'tool_evidence_source']}
    failed = finish(runtime, tasks[1], 'Reviewer failed partial', status='error')
    assert failed['status'] in {'failed', 'needs_attention'} and failed['review'] is None
    assert {k: failed[k] for k in executor_sources} == executor_sources
    assert all(source['task_id'] == tasks[0]['id'] for source in executor_sources.values())
    assert len(tasks) == 2


def test_program_receipt_freezes_failed_sources_without_passing_or_replaying(runtime):
    from lib.evolution import programs
    runs, _, tasks = runtime
    steps = [{'kind': 'discovery', 'title': 'Inspect the evidence',
              'instruction': 'Read the original.', 'checks': ['Evidence is read.']},
             {'kind': 'final', 'title': 'Audit the objective',
              'instruction': 'Complete all checks.', 'checks': ['All checks pass.']}]
    program = programs.create_program({'objective': 'Complete an evidence-bound exploration.',
        'steps': steps, 'auto_replan': False, 'max_attempts': 1}, user_id=OWNER,
        idempotency_key='failed-execution-sources')
    pid = program['program_id']
    programs.command_program(pid, {'action': 'resume'}, user_id=OWNER)
    programs.advance_program(pid, user_id=OWNER)
    active = programs.advance_program(pid, user_id=OWNER)
    task = tasks[0]
    task['messages'].append({'role': 'tool', 'tool_call_id': 'retained-tool',
                             'content': 'Failed experiment observation'})
    failed = finish(runtime, task, '', status='error')
    settled = programs.advance_program(pid, user_id=OWNER)
    receipt = programs.experiment_page(pid, user_id=OWNER)['experiments'][0]
    assert settled['status'] == 'needs_attention' and settled['queue'] == steps
    assert receipt['review'] is None
    assert receipt['result_source'] == failed['result_source']
    assert receipt['tool_evidence_source'] == failed['tool_evidence_source']
    assert receipt['result_source']['task_id'] == task['id']
    assert original(failed, receipt['tool_evidence_source'], 'tool_evidence')
    assert runs.get_run(active['current']['run_id'], user_id=OWNER) == failed
    assert programs.advance_program(pid, user_id=OWNER) == settled
    assert programs.experiment_page(pid, user_id=OWNER)['experiments'][0] == receipt
    assert len(tasks) == 1

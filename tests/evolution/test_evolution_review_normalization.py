"""Lossless format projection preserves judgment, source authority and cost."""
import hashlib
import json

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER

pytestmark = pytest.mark.unit


def ready(runtime, **options):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Audit retained evidence.', 'max_attempts': 1, **options}, user_id=OWNER)
    return finish(runtime, tasks[0], 'The original deliverable.')


@pytest.mark.parametrize('wrapper', ['', 'Verification summary.\n', '```json\n'])
@pytest.mark.parametrize('outcome', ['pass', 'fail', 'unknown'])
def test_projection_retains_complete_unicode_text_and_original_outcomes(runtime, wrapper, outcome):
    from lib.evolution import stage_outcomes, get_episode
    runs, _, tasks = runtime
    run = ready(runtime, budget={'max_tokens': 30}, acceptance_requirements=['Observe the original proof.'])
    value = json.loads(review(outcome))
    full = 'Observed evidence; unresolved prerequisites. 中文😀\n' * 30
    value['criterion_results'][0]['evidence_ref'] = full
    value['requirement_results'] = [{'requirement_id': 'requirement-1', 'outcome': outcome, 'evidence_ref': full}]
    value['consumer_assessment'] = {'meaningful_to_consumer': 'no', 'note': full}
    raw = ' \n' + wrapper + json.dumps(value, ensure_ascii=False) + ('\n```' if wrapper.startswith('```') else '') + '\n '
    task = tasks[-1]
    final = finish(runtime, task, raw)
    assert len(tasks) == 2 and final['attempt'] == 1 and final['actuals']['tokens'] == 30
    assert final['status'] == ('completed' if outcome == 'pass' else 'needs_attention')
    assert {r['outcome'] for r in final['review']['criterion_results']} == {outcome}
    marker = final['receipts'][-1]['review_format_normalization']
    assert marker['wrapped_json'] is bool(wrapper)
    assert marker['source']['source_sha256'] == hashlib.sha256(raw.encode()).hexdigest()
    assert marker['source']['total_chars'] == len(raw)
    original = stage_outcomes.page(run['run_id'], task['id'], user_id=OWNER)['content']
    assert original == raw
    parsed = json.loads(original[marker['json_offset']:marker['json_end']])
    assert parsed == value and parsed['criterion_results'][0]['evidence_ref'] == full
    for pointer in marker['field_paths']:
        assert pointer.startswith('/')
    assert marker['source']['source_sha256'] in final['review']['requirement_results'][0]['evidence_ref']
    assert 'review_format_retry' not in final['receipts'][-1]
    assert get_episode(run['episode_id'], user_id=OWNER)['actuals']['tokens'] == 30
    assert runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task) == final


def test_compact_wrapped_judgment_needs_no_new_model(runtime):
    _, _, tasks = runtime
    ready(runtime)
    final = finish(runtime, tasks[-1], 'Full source checked.\n' + review())
    assert final['status'] == 'completed' and len(tasks) == 2
    assert final['receipts'][-1]['review_format_normalization']['field_paths'] == []


@pytest.mark.parametrize('prefix,suffix', [('[]\n', ''), ('null\n', ''), ('{}\n', ''), ('', '\n{}'), ('', '\nActually this failed.'), ('Example {broken}\n', '')])
def test_ambiguous_or_incomplete_json_never_projects(runtime, prefix, suffix):
    _, _, tasks = runtime
    ready(runtime)
    final = finish(runtime, tasks[-1], prefix + review() + suffix)
    assert final['status'] == 'reviewing' and len(tasks) == 3
    assert 'review_format_normalization' not in final['receipts'][-1]


@pytest.mark.parametrize('case', ['missing_requirement', 'duplicate_criterion', 'unknown_field', 'invalid_outcome', 'duplicate_json_key'])
def test_contract_errors_cannot_hide_behind_format_projection(runtime, case):
    _, _, tasks = runtime
    ready(runtime, acceptance_requirements=['A required observation.'])
    value = json.loads(review())
    value['criterion_results'][0]['evidence_ref'] = 'x' * 524
    value['requirement_results'] = [{'requirement_id': 'requirement-1', 'outcome': 'pass', 'evidence_ref': 'Proof.'}]
    if case == 'missing_requirement': value.pop('requirement_results')
    if case == 'duplicate_criterion': value['criterion_results'][1]['criterion_id'] = 'objective'
    if case == 'unknown_field': value['repair'] = True
    if case == 'invalid_outcome': value['criterion_results'][1]['outcome'] = 'approved'
    raw = json.dumps(value)
    if case == 'duplicate_json_key': raw = raw.replace('"outcome": "pass"', '"outcome": "fail", "outcome": "pass"', 1)
    final = finish(runtime, tasks[-1], raw)
    assert final['status'] != 'completed'
    assert 'review_format_normalization' not in final['receipts'][-1]


@pytest.mark.parametrize('policy', [0, None])
def test_disabled_and_legacy_policies_keep_original_refusal(runtime, policy):
    runs, _, tasks = runtime
    run = ready(runtime, max_review_format_retries=0)
    if policy is None:
        saved, version = runs._read(run['run_id'], OWNER); saved.pop('max_review_format_retries')
        runs._save(saved, version, OWNER)
    final = finish(runtime, tasks[-1], 'Report.\n' + review())
    assert final['status'] == 'failed' and len(tasks) == 2
    assert 'review_format_normalization' not in final['receipts'][-1]


def test_format_projection_does_not_supply_unread_source_coverage(runtime):
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Audit complete original.', 'max_attempts': 1}, user_id=OWNER)
    tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'trace', 'content': 'x' * 8000})
    run = finish(runtime, tasks[0], 'Original deliverable.')
    assert run.get('tool_evidence_source')
    final = finish(runtime, tasks[-1], 'Report.\n' + review())
    assert final['status'] == 'needs_attention' and len(tasks) == 2
    assert final['review']['criterion_results'][0]['outcome'] == 'unknown'
    assert final['receipts'][-1]['review_format_normalization']


def test_cancelled_run_does_not_acquire_a_normalized_judgment(runtime):
    runs, _, tasks = runtime
    run = ready(runtime)
    runs.cancel_run(run['run_id'], user_id=OWNER)
    final = finish(runtime, tasks[-1], 'Report.\n' + review())
    assert final['status'] == 'cancelled' and final['review'] is None and len(tasks) == 2
    assert 'review_format_normalization' not in final['receipts'][-1]


def test_committed_projection_survives_ack_loss_without_rejudging(runtime, monkeypatch):
    from lib.storage.errors import StorageError
    runs, _, tasks = runtime
    run = ready(runtime)
    save = runs._save
    def lost_ack(value, version, user_id):
        result = save(value, version, user_id)
        if value['status'] == 'settling' and value['receipts'][-1].get('review_format_normalization'):
            raise StorageError('database_timeout', 'lost acknowledgement of committed projection')
        return result
    monkeypatch.setattr(runs, '_save', lost_ack)
    with pytest.raises(StorageError): finish(runtime, tasks[-1], 'Report.\n' + review())
    monkeypatch.setattr(runs, '_save', save)
    final = runs.advance_run(run['run_id'], user_id=OWNER)
    assert final['status'] == 'completed' and final['actuals']['tokens'] == 30 and len(tasks) == 2
    assert final['receipts'][-1]['review_format_normalization']


def test_complete_source_coverage_belongs_to_the_original_judge(runtime):
    from tests.evolution.test_evolution_source_tools import delivered
    runs, _, tasks = runtime
    runs.submit_run({'objective': 'Audit complete evidence.', 'max_attempts': 1}, user_id=OWNER)
    tasks[0]['messages'].append({'role': 'tool', 'tool_call_id': 'trace', 'content': 'x' * 8000})
    run = finish(runtime, tasks[0], 'Original deliverable.')
    source = run['tool_evidence_source']; offset = 0
    while True:
        result = delivered(tasks[-1], {'action': 'read', 'run_id': run['run_id'], 'task_id': source['task_id'],
            'source_kind': 'tool_evidence', 'source_sha256': source['source_sha256'], 'offset': offset})
        if result['next_offset'] is None: break
        offset = result['next_offset']
    final = finish(runtime, tasks[-1], 'Full original read.\n' + review())
    assert final['status'] == 'completed' and len(tasks) == 2
    assert final['receipts'][-1]['review_format_normalization']


def test_foreign_or_mismatched_original_cannot_authorize_projection(runtime, monkeypatch):
    runs, _, tasks = runtime
    run = ready(runtime); task = tasks[-1]
    # Parsing is not a public execution/authority entry. Even here the archive
    # and frozen launch binding refuse a changed original or another owner.
    from lib.evolution.contracts import EvolutionStateError
    parse = runs._parse_review
    def verify_bound_original(original_task, *, run, user_id):
        with pytest.raises(EvolutionStateError):
            parse(original_task, run=run, user_id=OWNER + 1)
        with pytest.raises(runs._ReviewFormatError):
            parse({**original_task, 'content': 'Different wrapper.\n' + review()}, run=run, user_id=user_id)
        return parse(original_task, run=run, user_id=user_id)
    monkeypatch.setattr(runs, '_parse_review', verify_bound_original)
    final = finish(runtime, task, 'Report.\n' + review())
    assert final['status'] == 'completed' and len(tasks) == 2


def test_concurrent_terminal_receivers_project_once_without_new_tasks(runtime, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    runs, _, tasks = runtime
    run = ready(runtime);task = tasks[-1]
    barrier = Barrier(2);normalize = runs._normalized_review
    def racing_normalization(*args, **kwargs):
        barrier.wait(timeout=10)
        return normalize(*args, **kwargs)
    monkeypatch.setattr(runs, '_normalized_review', racing_normalization)
    task.update(status='done', finishReason='stop', content='Report.\n' + review(),
                finished_at=task['created_at'] + 1, usage={'input_tokens': 10, 'output_tokens': 5})
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task), range(2)))
    # A losing receiver may observe the committed settling checkpoint before
    # the winning receiver completes the same episode.
    assert all(result['status'] in ('settling', 'completed') for result in results)
    final = runs.get_run(run['run_id'], user_id=OWNER)
    assert final['actuals']['tokens'] == 30 and len(tasks) == 2 and len(final['receipts']) == 2
    assert final['receipts'][-1]['review_format_normalization']

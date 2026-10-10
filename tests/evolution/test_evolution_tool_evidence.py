"""Full retained tool messages survive clipping, loss and independent review."""
import hashlib
import json

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, finish, review
from tests.evolution.test_evolution_source_tools import delivered

pytestmark = pytest.mark.unit
TOOLS = [
    {'role': 'tool', 'tool_call_id': 'observed-command-' + 'x' * 180,
     'content': 'Observed head\n' + '🧪e\u0301\\"\n' * 12000 + 'TAIL_COUNTEREXAMPLE'},
    {'role': 'assistant', 'content': 'A self-reported result is not a tool message.'},
    {'role': 'tool', 'tool_call_id': 'observed-image',
     'content': [{'type': 'text', 'text': 'Actual structured result <script>inert</script>'}]},
]


@pytest.fixture(autouse=True)
def no_external_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kwargs: [])


def started(runtime, tools=TOOLS):
    runs, _, tasks = runtime
    initial = runs.submit_run({'objective': 'Assess the observed counterexample.', 'max_attempts': 1},
                              user_id=OWNER)
    task = tasks[0]
    task['messages'] = tools
    return initial, task


def source_args(run, field):
    return {key: value for key, value in run[field].items() if key != 'total_chars'}


def read_all(reviewer, run, field):
    source = source_args(run, field)
    parts, offset = [], 0
    while True:
        result = delivered(reviewer, {'action': 'read', 'run_id': run['run_id'], **source,
            'offset': offset}, call_id=field + str(offset))
        parts.append(result['content'])
        if result['next_offset'] is None:
            return ''.join(parts)
        offset = result['next_offset']


def test_full_jsonl_retains_unicode_ids_and_structured_messages_without_fabricating_history(runtime):
    from lib.evolution import stage_outcomes, tool_evidence
    initial, executor = started(runtime)
    run = finish(runtime, executor, 'The tail contains a counterexample.')
    assert runtime[2][-1]['config']['toolSpecAllowlist'] == ['evolution_source', 'evolution_review']
    metadata = stage_outcomes.read_metadata(initial['run_id'], executor['id'], user_id=OWNER)
    assert metadata['tool_evidence']['scope'] == 'terminal_retained_tool_messages'
    assert metadata['tool_evidence']['message_count'] == 2
    assert metadata['tool_evidence']['preview_truncated']
    assert 'TAIL_COUNTEREXAMPLE' not in metadata['review_prompt']
    assert 'observed_tool_results_preview_only' in metadata['review_prompt']
    text = read_all(runtime[2][-1], run, 'tool_evidence_source')
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows == [{'message_index': 0, 'tool_call_id': TOOLS[0]['tool_call_id'], 'content': TOOLS[0]['content']},
                    {'message_index': 2, 'tool_call_id': TOOLS[2]['tool_call_id'], 'content': TOOLS[2]['content']}]
    assert hashlib.sha256(text.encode()).hexdigest() == metadata['tool_evidence']['content_sha256']
    assert all(len(part) <= 30000 for part in tool_evidence.chunks(executor))


def test_unread_clipped_tool_evidence_cannot_pass_an_aggregate_review(runtime):
    initial, executor = started(runtime)
    run = finish(runtime, executor, 'A short deliverable does not expose the full evidence.')
    assert 'result_source' not in run and run['tool_evidence_source']['task_id'] == executor['id']
    final = finish(runtime, runtime[2][-1], review())
    assert final['status'] == 'needs_attention'
    assert final['review']['criterion_results'][0]['outcome'] == 'unknown'
    assert 'tool evidence' in final['review']['criterion_results'][0]['evidence_ref']


@pytest.mark.parametrize('read_output', [False, True])
def test_delivery_and_tool_evidence_require_independent_successful_input_coverage(runtime, read_output):
    from lib.evolution.source_tools import fully_read
    initial, executor = started(runtime)
    content = 'Large observed report\n' * 1600
    run = finish(runtime, executor, content)
    reviewer = runtime[2][-1]
    assert json.loads(read_all(reviewer, run, 'tool_evidence_source').splitlines()[0])['content'] == TOOLS[0]['content']
    assert fully_read(reviewer, run['tool_evidence_source'])
    assert not fully_read(reviewer, run['result_source'])
    if read_output:
        assert read_all(reviewer, run, 'result_source') == content
    final = finish(runtime, reviewer, review())
    assert final['status'] == ('completed' if read_output else 'needs_attention')


def test_owner_page_is_read_only_and_fences_foreign_source_and_digest(runtime):
    from lib.evolution import stage_outcomes
    from lib.evolution.source_tools import execute
    initial, executor = started(runtime)
    run = finish(runtime, executor, 'Short delivery.')
    reviewer = runtime[2][-1]
    writes = runtime[1].writes
    first = stage_outcomes.page(run['run_id'], executor['id'], user_id=OWNER, source_kind='tool_evidence')
    assert first['source_kind'] == 'tool_evidence' and len(first['content']) == 6000
    assert first['tool_evidence_available'] and first['tool_evidence_scope'] == 'terminal_retained_tool_messages'
    assert runtime[1].writes == writes
    with pytest.raises((ValueError, RuntimeError)):
        stage_outcomes.page(run['run_id'], executor['id'], user_id=OWNER + 1, source_kind='tool_evidence')
    with pytest.raises((ValueError, RuntimeError)):
        execute(reviewer, {'action': 'read', 'run_id': run['run_id'],
            **source_args(run, 'tool_evidence_source'), 'source_sha256': hashlib.sha256(b'Short delivery.').hexdigest()})
    with pytest.raises((ValueError, RuntimeError)):
        execute(reviewer, {'action': 'read', 'run_id': run['run_id'],
            **source_args(run, 'tool_evidence_source'), 'source_kind': 'deliverable'})
    assert runtime[1].writes == writes


def test_all_evidence_parts_resume_after_seal_loss_without_executor_replay(runtime, monkeypatch):
    from lib.evolution import stage_outcomes, records, runs
    initial, executor = started(runtime)
    original = records.put_record
    def refuse_seal(namespace, key, value, **kwargs):
        if namespace == stage_outcomes.NAMESPACE and value['archive_state'] == 'sealed':
            raise RuntimeError('lost seal acknowledgement')
        return original(namespace, key, value, **kwargs)
    monkeypatch.setattr(records, 'put_record', refuse_seal)
    with pytest.raises(RuntimeError, match='seal'):
        finish(runtime, executor, 'Observed delivery.')
    monkeypatch.setattr(records, 'put_record', original)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    recovered = runs.advance_run(initial['run_id'], user_id=OWNER)
    assert recovered['status'] == 'reviewing' and len(runtime[2]) == 2
    assert 'TAIL_COUNTEREXAMPLE' in read_all(runtime[2][-1], recovered, 'tool_evidence_source')
    metadata = stage_outcomes.read_metadata(initial['run_id'], executor['id'], user_id=OWNER)
    assert metadata['archive_state'] == 'sealed'


def test_missing_tool_part_retains_known_usage_and_refuses_replay_after_owner_loss(runtime, monkeypatch):
    from lib.evolution import stage_outcomes, records, runs
    initial, executor = started(runtime)
    original = records.put_record
    def refuse_part(namespace, key, value, **kwargs):
        if namespace == stage_outcomes.PART_NAMESPACE and value.get('source_kind') == 'tool_evidence':
            raise RuntimeError('tool evidence write interrupted')
        return original(namespace, key, value, **kwargs)
    monkeypatch.setattr(records, 'put_record', refuse_part)
    with pytest.raises(RuntimeError, match='interrupted'):
        finish(runtime, executor, 'Observed delivery.')
    monkeypatch.setattr(records, 'put_record', original)
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    monkeypatch.setattr('lib.evolution.review_recovery.owner_state', lambda *args: 'dead')
    failed = runs.advance_run(initial['run_id'], user_id=OWNER)
    assert failed['status'] == 'needs_attention' and len(runtime[2]) == 1
    assert failed['actuals']['tokens'] == 15 and len(failed['receipts']) == 1
    assert 'archive is incomplete' in failed['error']


def test_legacy_output_remains_readable_without_inventing_tool_evidence(runtime):
    from lib.evolution import stage_outcomes, records
    initial, executor = started(runtime, tools=[])
    run = finish(runtime, executor, 'An older original.')
    metadata = stage_outcomes.read_metadata(initial['run_id'], executor['id'], user_id=OWNER)
    metadata.pop('tool_evidence')
    records.put_record(stage_outcomes.NAMESPACE, records.entity_key(executor['id'], OWNER, kind='stage outcome'),
                       metadata, kind='stage outcome')
    assert stage_outcomes.page(run['run_id'], executor['id'], user_id=OWNER)['content'] == 'An older original.'
    with pytest.raises((ValueError, RuntimeError)):
        stage_outcomes.page(run['run_id'], executor['id'], user_id=OWNER, source_kind='tool_evidence')


def test_a_tool_producer_read_without_successful_provider_delivery_cannot_pass(runtime):
    from lib.evolution.source_tools import execute, fully_read
    initial, executor = started(runtime, tools=[{'role': 'tool', 'tool_call_id': 'probe', 'content': 'x' * 2500}])
    run = finish(runtime, executor, 'Short delivery.')
    reviewer = runtime[2][-1]
    result = execute(reviewer, {'action': 'read', 'run_id': run['run_id'], **source_args(run, 'tool_evidence_source')})
    assert result['next_offset'] is None
    assert not fully_read(reviewer, run['tool_evidence_source'])
    final = finish(runtime, reviewer, review())
    assert final['status'] == 'needs_attention'


def test_full_tool_coverage_is_frozen_and_recoverable_without_replaying_review(runtime, monkeypatch):
    from lib.evolution import stage_outcomes, runs
    from lib.evolution.source_tools import fully_read
    initial, executor = started(runtime, tools=[{'role': 'tool', 'tool_call_id': 'probe', 'content': 'x' * 2500}])
    run = finish(runtime, executor, 'Short delivery.')
    reviewer = runtime[2][-1]
    read_all(reviewer, run, 'tool_evidence_source')
    reviewer.update(status='done', finishReason='stop', content=review(),
                    finished_at=reviewer['created_at'] + 1, usage={'input_tokens': 10, 'output_tokens': 5})
    stage_outcomes.capture(run, reviewer, user_id=OWNER, review_prompt_factory=lambda content: None)
    metadata, restored = stage_outcomes.recover(run, user_id=OWNER)
    assert metadata['tool_evidence_coverage']['source'] == run['tool_evidence_source']
    assert fully_read(restored, run['tool_evidence_source'])
    monkeypatch.setattr(runs, 'default_task_loader', lambda *args, **kwargs: None)
    final = runs.advance_run(run['run_id'], user_id=OWNER)
    assert final['status'] == 'completed' and len(runtime[2]) == 2

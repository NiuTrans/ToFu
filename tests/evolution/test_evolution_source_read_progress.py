"""Acknowledged original access guides reviews without conferring verdicts.

Real run, archive, tool and request-budget boundaries; synthetic provider usage.
"""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.evolution.test_evolution_runs import runtime as runtime, finish, review, OWNER

pytestmark = pytest.mark.unit
CONTENT = 'Original counterexample 🧪e\u0301\n' * 1200


@pytest.fixture(autouse=True)
def no_reindex(monkeypatch):
    monkeypatch.setattr('lib.evolution.maintenance.reindex_maintenance', lambda **kw: [])


def start_review(runtime):
    run = runtime[0].submit_run({'objective': 'Assess the original counterexample.',
                                'max_attempts': 1}, user_id=OWNER)
    run = finish(runtime, runtime[2][-1], CONTENT)
    return run, runtime[2][-1]


def args(run, source=None, offset=0):
    source = source or run['result_source']
    return {'action': 'read', 'run_id': run['run_id'], 'task_id': source['task_id'],
            'source_sha256': source['source_sha256'], 'offset': offset,
            **({'source_kind': source['source_kind']} if source.get('source_kind') else {})}


def produce(task, request, call_id):
    from lib.evolution.source_tools import handle
    entry = handle(task, None, 'evolution_source', call_id, request, 1, {},
                   task['config'], '', False)[0]
    assert entry['fetched'], entry
    return entry, json.loads(entry['snippet'])


def send(task, entry, *, compressed=False, fail=False):
    from lib.llm.request import ModelRequestBudgetScope, mark_model_request_dispatched
    from lib.agent_core.execution_budget import model_budget_admission
    text = entry['title'] + ': ' + entry['snippet']
    with ModelRequestBudgetScope({'_model_budget_admit': model_budget_admission(task)}) as scope:
        lease = scope.body['_model_budget_admit'](input_tokens=1, max_output_tokens=1)
        mark_model_request_dispatched(SimpleNamespace(model_budget_lease=lease,
            body={'messages': [{'role': 'tool', 'content': text[:100] if compressed else text}]}))
        scope.usage = {'input_tokens': 1, 'output_tokens': 1}
        if fail:
            error = RuntimeError('injected metered provider failure')
            error.usage = scope.usage
            raise error


def progress(task):
    from lib.evolution.source_tools import execute
    return execute(task, {'action': 'list'})['review_read_progress']


def read_all(task, run, source):
    offset = 0
    while True:
        entry, page = produce(task, args(run, source, offset), f'{source.get("source_kind", "deliverable")}-{offset}')
        send(task, entry)
        if page['next_offset'] is None:
            break
        offset = page['next_offset']


def test_pending_failed_and_compressed_pages_do_not_advance_feedback(runtime):
    from lib.evolution.source_tools import execute
    run, task = start_review(runtime)
    rows = deepcopy(runtime[1].rows)
    assert progress(task)['sources'][0]['next_unread_offset'] == 0
    produced = execute(task, args(run))
    assert produced['review_read_progress']['sources'][0]['delivered_chars'] == 0
    entry, page = produce(task, args(run), 'pending')
    assert page['review_read_progress'] == progress(task)
    with pytest.raises(RuntimeError, match='injected'):
        send(task, entry, fail=True)
    assert progress(task)['sources'][0]['delivered_chars'] == 0
    send(task, entry, compressed=True)
    assert progress(task)['sources'][0]['delivered_chars'] == 0
    entry, page = produce(task, args(run), 'received')
    assert page['review_read_progress']['sources'][0]['delivered_chars'] == 0
    send(task, entry)
    assert progress(task)['sources'][0]['delivered_chars'] == 6000
    assert progress(task)['sources'][0]['next_unread_offset'] == 6000
    # Model dispatch/terminal receipts are durable; source access cannot mutate
    # the run, episode, archived source or any other authority.
    assert {key: value for key, value in runtime[1].rows.items()
            if key[0] != 'evolution.model_attempts.v1'} == rows


def test_overlap_and_tail_leave_the_first_gap_and_rereads_remain_available(runtime):
    run, task = start_review(runtime)
    for offset in [0, 1, len(CONTENT) - 6000]:
        entry, _ = produce(task, args(run, offset=offset), str(offset))
        send(task, entry)
    before = progress(task)
    assert before['sources'][0]['next_unread_offset'] == 6001
    assert before['sources'][0]['delivered_chars'] == 12001
    assert not before['all_sources_delivered']
    entry, repeated = produce(task, args(run), 'repeat')
    assert repeated['content'] == CONTENT[:6000]
    send(task, entry)
    assert progress(task) == before
    read_all(task, run, run['result_source'])
    final = progress(task)
    assert final['all_sources_delivered']
    assert final['sources'][0]['next_unread_offset'] is None
    assert final['sources'][0]['delivered_chars'] == len(CONTENT)
    assert final['next_action'] == 'submit_independent_review'
    assert finish(runtime, task, review('fail'))['status'] == 'needs_attention'


def test_all_three_frozen_sources_require_distinct_acknowledgments(runtime, tmp_path):
    from lib.evolution.artifact_evidence import declare
    runtime[0].submit_run({'objective': 'Assess this artifact independently.',
        'project_path': str(tmp_path), 'max_attempts': 1}, user_id=OWNER)
    executor = runtime[2][-1]
    (tmp_path / 'claim.json').write_text('{"claim": "requires evidence"}')
    declare(executor, {'path': 'claim.json'})
    executor['messages'] = [{'role': 'tool', 'tool_call_id': 'observed',
                             'content': 'Observed counterexample, not a verified claim.'}]
    run = finish(runtime, executor, 'An artifact is ready for review.')
    task = runtime[2][-1]
    sources = [row['source'] for row in progress(task)['sources']]
    assert {s.get('source_kind', 'deliverable') for s in sources} == {
        'deliverable', 'tool_evidence', 'artifact_evidence'}
    for index, source in enumerate(sources):
        read_all(task, run, source)
        current = progress(task)
        assert sum(row['fully_delivered'] for row in current['sources']) == index + 1
        assert current['all_sources_delivered'] == (index == 2)
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['review'] is None
    # Having read everything does not turn a substantive unknown into success.
    assert finish(runtime, task, review('unknown'))['status'] == 'needs_attention'


@pytest.mark.parametrize('corruption', ['foreign_digest', 'beyond_end', 'overlap'])
def test_foreign_or_invalid_coverage_and_stale_current_owner_refuse_feedback(runtime, corruption):
    from lib.evolution.source_tools import execute
    run, task = start_review(runtime)
    entry, _ = produce(task, args(run), 'received')
    send(task, entry)
    valid = deepcopy(task['_evolutionSourceCoverage'])
    if corruption == 'foreign_digest':
        task['_evolutionSourceCoverage']['source']['source_sha256'] = '0' * 64
    elif corruption == 'beyond_end':
        task['_evolutionSourceCoverage']['ranges'] = [[0, len(CONTENT) + 1]]
    else:
        task['_evolutionSourceCoverage']['ranges'] = [[0, 100], [50, 150]]
    with pytest.raises(RuntimeError):
        progress(task)
    task['_evolutionSourceCoverage'] = valid
    current, version = runtime[0]._read(run['run_id'], OWNER)
    current['task_id'] = 'successor-review'
    runtime[0]._save(current, version, OWNER)
    with pytest.raises(RuntimeError):
        execute(task, {'action': 'list'})


def test_executor_source_list_contains_no_reviewer_progress(runtime):
    from lib.evolution.source_tools import execute
    runtime[0].submit_run({'objective': 'A fresh executor.'}, user_id=OWNER)
    executor = runtime[2][-1]
    assert 'review_read_progress' not in execute(executor, {'action': 'list'})


def test_progress_is_reconstructed_from_sealed_receipts_without_a_new_task(runtime):
    from lib.evolution.stage_outcomes import capture, recover
    run, task = start_review(runtime)
    read_all(task, run, run['result_source'])
    expected = deepcopy(task['_evolutionSourceCoverage'])
    count = len(runtime[2])
    task.update(status='done', finishReason='stop', content=review('unknown'),
                usage={'input_tokens': 10, 'output_tokens': 5},
                finished_at=task['created_at'] + 1)
    capture(run, task, user_id=OWNER, review_prompt_factory=lambda _: None)
    task['_evolutionSourceCoverage']['ranges'] = []
    _, restored = recover(run, user_id=OWNER)
    assert restored['_evolutionSourceCoverage'] == expected
    with pytest.raises(RuntimeError, match='terminal goal task'):
        progress(restored)
    assert len(runtime[2]) == count
    settled = runtime[0].advance_run(run['run_id'], user_id=OWNER, terminal_task=restored)
    assert settled['status'] == 'needs_attention'

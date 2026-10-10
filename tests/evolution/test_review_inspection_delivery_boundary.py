"""Source access debt stays in the reviewer instead of replaying execution."""
from copy import deepcopy
import json

import pytest

from lib.evolution import review_delivery as delivery
from lib.evolution.contracts import EvolutionStateError
from tests.evolution.test_evolution_review_delivery import (
    runtime as runtime, request, read_originals, acknowledge, no_reindex, OWNER,
)
from tests.evolution.test_evolution_source_tools import delivered

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('outcome', ['pass', 'unknown'])
def test_uninspected_judgment_cannot_close_or_create_an_executor_repair(runtime, outcome):
    run, task, args, message = request(runtime, outcome=outcome, read_sources=False)
    before = deepcopy(args)
    writes = runtime[1].writes
    with pytest.raises(EvolutionStateError, match='inspection is unfinished') as refused:
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    progress = json.loads(str(refused.value)[str(refused.value).index('{'):])
    assert not progress['all_sources_delivered']
    assert progress['next_action'] == 'read_missing_sources'
    assert progress['sources'][0]['next_unread_offset'] == 0
    assert '_explicitToolDeliveryRequest' not in task and args == before
    assert runtime[1].writes == writes
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert len(runtime[2]) == 2


def test_partial_delivery_returns_the_exact_next_gap_and_preserves_the_author(runtime):
    run, task, args, message = request(runtime, read_sources=False)
    source = run['result_source']
    delivered(task, {'action': 'read', 'run_id': run['run_id'], 'task_id': source['task_id'],
                     'source_sha256': source['source_sha256'], 'offset': 0}, call_id='first-page')
    with pytest.raises(EvolutionStateError) as refused:
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    progress = json.loads(str(refused.value)[str(refused.value).index('{'):])
    assert progress['sources'][0]['next_unread_offset'] == 6000
    assert progress['sources'][0]['delivered_chars'] == 6000
    assert task['id'] == runtime[2][-1]['id'] and len(runtime[2]) == 2


@pytest.mark.parametrize('outcome', ['pass', 'unknown'])
def test_the_same_reviewer_can_finish_after_acknowledged_source_delivery(runtime, outcome):
    run, task, args, message = request(runtime, outcome=outcome, read_sources=False)
    with pytest.raises(EvolutionStateError):
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    read_originals(run, task)
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, result)
    assert json.loads(task['content']) == args['review']
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert len(runtime[2]) == 2


def test_a_material_failure_can_finish_without_all_positive_acceptance_sources(runtime):
    run, task, args, message = request(runtime, outcome='fail', read_sources=False)
    before = deepcopy(args)
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, result) and args == before
    assert json.loads(task['content']) == before['review']


def test_unverified_audit_notes_do_not_weaken_a_definite_failed_check(runtime):
    run, task, args, message = request(runtime, outcome='fail', read_sources=False)
    args['review']['evidence_audit']['complete'] = False
    args['review']['evidence_audit']['objections'] = [{'criterion_id': criterion,
        'classification': 'unverified', 'claim': 'This source was not inspected.',
        'evidence_ref': 'No failed source read is observed.'} for criterion in ['objective', 'evidence', 'delivery']]
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    before = deepcopy(args)
    result = delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert acknowledge(task, message, result) and args == before


def test_preparation_projection_does_not_record_a_consumer_judgment(runtime):
    run, task, args, message = request(runtime, read_sources=False)
    args['review']['consumer_assessment'] = {'meaningful_to_consumer': 'no',
                                            'note': 'Observed demand remains unknown.'}
    message['tool_calls'][0]['function']['arguments'] = json.dumps(args)
    writes = runtime[1].writes
    with pytest.raises(EvolutionStateError):
        delivery.prepare(task, args, call_id='review-call', round_num=0)
    assert runtime[1].writes == writes


@pytest.mark.parametrize('outcome', ['pass', 'unknown'])
def test_wire_refusal_delivers_complete_gaps_to_the_same_reviewer(runtime, monkeypatch, outcome):
    from lib.tools.registry._build import _handle_evolution_teach
    run, task, args, message = request(runtime, outcome=outcome, read_sources=False)
    monkeypatch.setattr('lib.tasks_pkg.executor._finalize_tool_round', lambda *args: None)
    before = deepcopy(args)
    _, text, _ = _handle_evolution_teach(task, None, delivery.NAME, 'review-call', args, 0,
        {'llmRound': 0}, task['config'], '', False)
    progress = json.loads(text[text.index('{'):])
    assert progress['next_action'] == 'read_missing_sources'
    expected = [run[key] for key in ['result_source', 'tool_evidence_source', 'artifact_evidence_source'] if run.get(key)]
    assert len(progress['sources']) == len(expected)
    for source, original in zip(progress['sources'], expected):
        assert source['source']['task_id'] == original['task_id']
        assert source['source']['source_sha256'] == original['source_sha256']
        assert source['next_unread_offset'] == 0
    assert args == before and '_explicitToolDeliveryRequest' not in task
    assert runtime[0].get_run(run['run_id'], user_id=OWNER)['status'] == 'reviewing'
    assert len(runtime[2]) == 2


def test_other_tool_errors_keep_their_compact_preview(runtime, monkeypatch):
    run, task, args, message = request(runtime, read_sources=False)
    def ordinary_error(*args, **kwargs):
        raise EvolutionStateError('Untrusted failure details: ' + 'x' * 1000)
    monkeypatch.setattr(delivery, 'prepare', ordinary_error)
    entries = delivery.handle(task, None, delivery.NAME, 'review-call', args, 0,
        {'llmRound': 0}, task['config'], '', False)
    assert len(entries) == 1 and len(entries[0]['snippet']) == 400
    assert not entries[0]['fetched'] and '_explicitToolDeliveryRequest' not in task

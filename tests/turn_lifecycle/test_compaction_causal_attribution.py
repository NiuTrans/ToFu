"""Compaction starts are causal boundaries; task models are never summaries."""
from copy import deepcopy
import json

import pytest

from lib.agent_core.events import EventType, Phase
from lib.turn_activity_timeline import (
    compaction_attribution_from_receipt, fold_activity_timeline,
    normalize_activity_timeline,
)


pytestmark = pytest.mark.unit

def task(attempt='attempt-a', segments=None):
    return {'id': 'task-a', '_attemptId': attempt, 'model': 'Sol',
            'segments': segments or [], '_activityLastLlmRound': 0}


def segment(block, attempt='attempt-a', round_num=0):
    return {'type': 'text', 'blockId': block, 'text': 'output',
            'taskId': 'task-a', 'attemptId': attempt, 'llmRound': round_num}


def fold(timeline, owner, event_type, seq, **fields):
    if fields.get('receipt', {}).get('summary', {}).get('cost', {}).get('calls'):
        fields.setdefault('summaryDispatchCalls', [
            {key: row[key] for key in ('model', 'providerId') if key in row}
            for row in fields['receipt']['summary']['cost']['calls']
        ])
    return fold_activity_timeline(timeline, {
        'type': event_type, 'seq': seq, 'emittedAt': seq * 1000, **fields,
    }, owner)


def receipt(model='Luna', selected='chosen-Luna', status='completed'):
    return {'status': status, 'summary': {
        'route': {'modelId': selected, 'providerId': 'selected-provider'},
        'cost': {'callCount': 1, 'calls': [{
            'model': model, 'providerId': 'actual-provider', 'status': status,
            'known': True, 'costUsd': .02, 'costBasis': 'metered_api_equivalent',
            'pricingSource': 'provider_override', 'pricingSnapshot': {
                'model': model, 'provider': 'actual-provider', 'currency': 'USD',
                'source': 'provider_override', 'rates': {'input': 2., 'output': 3.},
            },
        }]},
    }}


@pytest.mark.parametrize('round_num', [0, 1, 17])
def test_preflight_zero_based_round_and_task_summary_identity(round_num):
    owner = task()
    timeline = fold(None, owner, EventType.PHASE, 1, phase=Phase.COMPACTING)
    timeline = fold(timeline, owner, EventType.COMPACTION, 2,
                    archiveId='archive-a', roundNum=round_num, model='Sol')
    started = deepcopy(timeline['entries'][0])
    owner['segments'] = [segment('future-answer', round_num=round_num)]
    timeline = fold(timeline, owner, EventType.COMPACTION_DONE, 3,
                    archiveId='archive-a', roundNum=round_num, receipt=receipt())
    entry = timeline['entries'][0]
    assert entry['anchorPosition'] == started['anchorPosition'] == 'producer-start'
    assert 'anchorBlockId' not in entry
    assert entry['llmRound'] == round_num
    assert entry['taskModel'] == 'Sol'
    assert entry['summaryModel'] == 'Luna'
    assert entry['summarySelectedModel'] == 'chosen-Luna'
    assert entry['summaryProviderId'] == 'actual-provider'
    assert 'model' not in entry
    assert normalize_activity_timeline(json.loads(json.dumps(timeline))) == timeline


def test_midturn_anchor_does_not_slide_over_future_same_round_output():
    owner = task(segments=[segment('prior'), {'type': 'tool_use', 'blockId': 'tool-1',
        'taskId': 'task-a', 'attemptId': 'attempt-a', 'roundNum': 13, 'llmRound': 0}])
    timeline = fold(None, owner, EventType.PHASE, 1, phase=Phase.COMPACTING)
    owner['segments'].append(segment('future'))
    timeline = fold(timeline, owner, EventType.COMPACTION, 2, archiveId='archive', roundNum=0)
    timeline = fold(timeline, owner, EventType.COMPACTION_DONE, 3,
                    archiveId='archive', roundNum=0, receipt=receipt(),
                    anchorPosition='after', anchorBlockId='future')
    assert timeline['entries'][0]['anchorBlockId'] == 'tool-1'
    assert timeline['entries'][0]['anchorPosition'] == 'after'


def test_repeated_compaction_round_and_resumed_counter_are_distinct():
    owner = task(segments=[segment('first')])
    timeline = None
    for index, block in enumerate(['first', 'second']):
        owner['segments'] = [segment(block)]
        base = index * 3
        timeline = fold(timeline, owner, EventType.PHASE, base + 1, phase=Phase.COMPACTING)
        timeline = fold(timeline, owner, EventType.COMPACTION, base + 2,
                        archiveId=f'archive-{index}', roundNum=0)
        timeline = fold(timeline, owner, EventType.COMPACTION_DONE, base + 3,
                        archiveId=f'archive-{index}', roundNum=0, receipt=receipt())
    resumed = task('attempt-b', [segment('old', attempt='attempt-a')])
    timeline = fold(timeline, resumed, EventType.PHASE, 7, phase=Phase.COMPACTING)
    timeline = fold(timeline, resumed, EventType.COMPACTION_DONE, 8,
                    archiveId='resume', roundNum=0, receipt=receipt(status='failed'))
    entries = timeline['entries']
    assert len(entries) == 3
    assert [e.get('anchorBlockId') for e in entries] == ['first', 'second', None]
    assert entries[-1]['anchorPosition'] == 'producer-start'
    assert entries[-1]['attemptId'] == 'attempt-b'
    assert entries[-1]['status'] == 'failed'
    assert len({e['id'] for e in entries}) == 3


@pytest.mark.parametrize('evidence', [{}, {'summary': {'route': {'modelId': 'selected-only'}}}])
def test_sparse_legacy_history_does_not_guess_dispatch_or_anchor(evidence):
    owner = task(segments=[segment('already-rendered-future')])
    timeline = fold(None, owner, EventType.COMPACTION_DONE, 1,
                    archiveId='historical', roundNum=0, model='Sol', receipt=evidence)
    entry = timeline['entries'][0]
    assert entry['taskModel'] == 'Sol'
    assert 'summaryModel' not in entry
    assert 'summaryProviderId' not in entry
    assert 'anchorBlockId' not in entry
    assert entry['anchorPosition'] == 'producer-start'


def test_failed_summary_retains_recorded_provider_and_cost_without_mutation():
    original = receipt(status='failed')
    evidence = deepcopy(original)
    projected = compaction_attribution_from_receipt(evidence, evidence['summary']['cost']['calls'])
    assert evidence == original
    assert projected['summaryCalls'] == original['summary']['cost']['calls']
    assert projected['summaryModel'] == 'Luna'
    assert projected['summaryProviderId'] == 'actual-provider'


def test_bounded_receipt_seam_strips_secrets_and_keeps_mixed_calls_honest():
    evidence = receipt()
    calls = evidence['summary']['cost']['calls']
    calls[0]['apiKey'] = 'must-not-project'
    calls[0]['pricingSnapshot']['secret'] = 'must-not-project'
    calls.append({'model': 'different-summary', 'providerId': 'different-provider', 'known': False})
    evidence['summary']['cost']['callCount'] = 2
    projected = compaction_attribution_from_receipt(evidence, evidence['summary']['cost']['calls'])
    assert len(projected['summaryCalls']) == 2
    assert 'must-not-project' not in json.dumps(projected)
    assert 'summaryModel' not in projected
    assert 'summaryProviderId' not in projected
    evidence['summary']['cost']['calls'] *= 100
    evidence['summary']['cost']['callCount'] = 200
    assert len(compaction_attribution_from_receipt(evidence)['summaryCalls']) == 16


@pytest.mark.parametrize('position', [[], {}, 7, True, ['after']])
def test_hostile_anchor_position_is_rejected_without_crashing(position):
    timeline = fold(None, task(), EventType.COMPACTION_DONE, 1,
                    archiveId='history', roundNum=0, receipt={})
    timeline['entries'][0]['anchorPosition'] = position
    assert normalize_activity_timeline(timeline) is None


def test_late_done_does_not_consume_new_same_round_start():
    owner = task(segments=[segment('first')])
    timeline = fold(None, owner, EventType.PHASE, 1, phase=Phase.COMPACTING)
    timeline = fold(timeline, owner, EventType.COMPACTION, 2, archiveId='first', roundNum=0)
    owner['segments'] = [segment('second')]
    timeline = fold(timeline, owner, EventType.PHASE, 3, phase=Phase.COMPACTING)
    timeline = fold(timeline, owner, EventType.COMPACTION_DONE, 4,
                    archiveId='first', roundNum=0, receipt=receipt())
    assert len(timeline['entries']) == 2
    timeline = fold(timeline, owner, EventType.COMPACTION, 5, archiveId='second', roundNum=0)
    entries = timeline['entries']
    assert len(entries) == 2
    assert [e['anchorBlockId'] for e in entries] == ['first', 'second']


def test_existing_legacy_archive_span_is_joined_without_duplicate():
    timeline = fold(None, task(), EventType.COMPACTION, 1, archiveId='old', roundNum=0)
    timeline['entries'][0]['spanId'] = 'legacy-carrier-derived-span'
    stable_id = timeline['entries'][0]['id']
    timeline = fold(timeline, task(), EventType.COMPACTION_DONE, 2,
                    archiveId='old', roundNum=0, receipt=receipt())
    assert len(timeline['entries']) == 1
    assert timeline['entries'][0]['id'] == stable_id
    assert timeline['entries'][0]['spanId'] == 'legacy-carrier-derived-span'


def test_terminal_alias_and_foreign_producer_do_not_become_start_boundaries():
    owner = task(segments=[segment('terminal:mutable'),
        segment('older-producer', attempt='attempt-b')])
    timeline = fold(None, owner, EventType.PHASE, 1, phase=Phase.COMPACTING)
    assert timeline['entries'][0]['anchorPosition'] == 'producer-start'
    assert 'anchorBlockId' not in timeline['entries'][0]


def test_legacy_receipt_call_model_is_not_dispatch_proof():
    evidence = receipt()
    projected = compaction_attribution_from_receipt(evidence)
    assert projected['summarySelectedModel'] == 'chosen-Luna'
    assert 'summaryModel' not in projected
    assert 'summaryProviderId' not in projected
    assert 'model' not in projected['summaryCalls'][0]
    assert projected['summaryCalls'][0]['pricingSnapshot'] == evidence['summary']['cost']['calls'][0]['pricingSnapshot']


def test_event_source_uses_observed_dispatch_not_selected_route_fallback():
    from lib.tasks_pkg.compaction._archive import summary_dispatch_calls
    usage = {'_summaryRoute': {'modelId': 'selected', 'providerId': 'selected-provider'},
             '_summaryCalls': [
                 {'model': 'selected', 'provider_id': 'selected-provider', 'usage': {}},
                 {'model': 'selected', 'usage': {'_dispatch': {
                     'model': 'actual', 'provider_id': 'actual-provider', 'api_key': 'must-not-project'}}},
             ]}
    assert summary_dispatch_calls(usage) == [{}, {'model': 'actual', 'providerId': 'actual-provider'}]
    assert len(summary_dispatch_calls({'_summaryCalls': usage['_summaryCalls'] * 100})) == 16
    evidence = receipt()
    evidence['summary']['cost']['callCount'] = 2
    evidence['summary']['cost']['calls'].append({'known': False})
    projected = compaction_attribution_from_receipt(evidence, summary_dispatch_calls(usage))
    assert 'summaryModel' not in projected  # First attempt identity unknown.
    assert projected['summaryCalls'][1]['model'] == 'actual'


def test_reactive_archive_then_phase_then_done_has_one_witnessed_boundary():
    owner = task(segments=[segment('before-reactive-summary')])
    timeline = fold(None, owner, EventType.COMPACTION, 1, archiveId='reactive', roundNum=0)
    timeline = fold(timeline, owner, EventType.PHASE, 2, phase=Phase.COMPACTING)
    owner['segments'].append(segment('future'))
    timeline = fold(timeline, owner, EventType.COMPACTION_DONE, 3,
                    archiveId='reactive', roundNum=0, receipt=receipt())
    assert len(timeline['entries']) == 1
    assert timeline['entries'][0]['anchorBlockId'] == 'before-reactive-summary'
    assert timeline['entries'][0]['anchorPosition'] == 'after'
    assert timeline['entries'][0]['status'] == 'succeeded'


def test_real_no_dispatch_receipt_cannot_attribute_selected_model_as_actual():
    from lib.tasks_pkg.compaction._receipt import build_compaction_receipt
    evidence = build_compaction_receipt(
        trigger='reactive', status='failed', strategy='none',
        implementation='summary_not_adopted', summary_generated=False,
        summary_usage={'_summaryRoute': {'modelId': 'not-dispatched', 'providerId': 'not-dispatched-provider'}},
    )
    timeline = fold(None, task(), EventType.COMPACTION_DONE, 1, archiveId='not-dispatched',
                    roundNum=0, receipt=evidence, summaryDispatchCalls=[])
    entry = timeline['entries'][0]
    assert entry['summarySelectedModel'] == 'not-dispatched'
    assert 'summaryModel' not in entry
    assert 'summaryProviderId' not in entry
    assert all('model' not in row and 'providerId' not in row for row in entry.get('summaryCalls', []))


def test_new_attribution_container_and_free_text_secrets_are_not_projected():
    timeline = fold(None, task(), EventType.COMPACTION_DONE, 1, archiveId='safe', roundNum=0,
                    receipt={})
    entry = timeline['entries'][0]
    entry.update(summarySelectedModel={'apiKey': 'SYNTHETIC_SECRET'},
        taskModel=['SYNTHETIC_SECRET'], summaryProviderId={'apiKey': 'SYNTHETIC_SECRET'},
        summaryCalls=[{'model': {'apiKey': 'SYNTHETIC_SECRET'}, 'providerId': False,
                      'reason': 'Authorization: Bearer SYNTHETIC_SECRET',
                      'pricingSnapshot': {'model': {'secret': 'SYNTHETIC_SECRET'},
                                          'source': 'token=SYNTHETIC_SECRET'}}])
    normalized = normalize_activity_timeline(timeline)
    assert 'SYNTHETIC_SECRET' not in json.dumps(normalized)
    safe = normalized['entries'][0]
    assert 'summarySelectedModel' not in safe
    assert 'taskModel' not in safe
    assert 'summaryProviderId' not in safe
    assert 'model' not in safe['summaryCalls'][0]
    assert safe['summaryCalls'][0]['reason'] == '<redacted>'


@pytest.mark.parametrize('call_count', [None, 0, True, 1.0, '1', -1, 17])
def test_incomplete_or_invalid_call_count_never_establishes_actual_identity(call_count):
    evidence = receipt()
    if call_count is None:
        evidence['summary']['cost'].pop('callCount')
    else:
        evidence['summary']['cost']['callCount'] = call_count
    observed = [{'model': 'Luna', 'providerId': 'actual-provider'}]
    projected = compaction_attribution_from_receipt(evidence, observed)
    assert 'summaryModel' not in projected
    assert 'summaryProviderId' not in projected
    assert all('model' not in row and 'providerId' not in row
               for row in projected['summaryCalls'])


@pytest.mark.parametrize('call_count', [None, 16, 17])
def test_oversized_observed_ledger_cannot_hide_a_different_last_route(call_count):
    evidence = receipt()
    evidence['summary']['cost']['calls'] *= 16
    if call_count is None:
        evidence['summary']['cost'].pop('callCount')
    else:
        evidence['summary']['cost']['callCount'] = call_count
    observed = [{'model': 'Luna', 'providerId': 'actual-provider'}] * 16 + [
        {'model': 'different-model', 'providerId': 'different-provider'}]
    projected = compaction_attribution_from_receipt(evidence, observed)
    assert len(projected['summaryCalls']) == 16
    assert 'summaryModel' not in projected
    assert 'summaryProviderId' not in projected
    assert all('model' not in row and 'providerId' not in row
               for row in projected['summaryCalls'])


@pytest.mark.parametrize('observed', [None, {}, [None], [{}, None], [receipt(), []]])
def test_filtered_or_malformed_call_evidence_cannot_become_complete(observed):
    projected = compaction_attribution_from_receipt(receipt(), observed)
    assert 'summaryModel' not in projected
    assert 'summaryProviderId' not in projected


@pytest.mark.parametrize('secret_text', [
    '{"apiKey":"SYNTHETIC_RECHECK_SECRET"}',
    '{"access_token": "SYNTHETIC_RECHECK_SECRET"}',
    "{'password': 'SYNTHETIC_RECHECK_SECRET'}",
    r'{\"refreshToken\":\"SYNTHETIC_RECHECK_SECRET\"}',
])
def test_quoted_credential_text_is_redacted_in_receipt_and_normalized_calls(secret_text):
    evidence = receipt()
    evidence['summary']['cost']['calls'][0].update(reason=secret_text)
    evidence['summary']['cost']['calls'][0]['pricingSnapshot']['source'] = secret_text
    projected = compaction_attribution_from_receipt(
        evidence, [{'model': 'Luna', 'providerId': 'actual-provider'}])
    assert 'SYNTHETIC_RECHECK_SECRET' not in json.dumps(projected)
    timeline = fold(None, task(), EventType.COMPACTION_DONE, 1,
                    archiveId='safe-text', roundNum=0, receipt={})
    timeline['entries'][0]['summaryCalls'] = evidence['summary']['cost']['calls']
    normalized = normalize_activity_timeline(timeline)
    assert 'SYNTHETIC_RECHECK_SECRET' not in json.dumps(normalized)
    assert normalized['entries'][0]['summaryCalls'][0]['reason'] == '<redacted>'

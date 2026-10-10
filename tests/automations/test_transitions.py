"""Interrupted external effects remain visible until their result is reconciled."""

import json

import pytest

from lib.automations.definitions import prepare_definition
from lib.automations.errors import AutomationConflict, AutomationError
from lib.automations.repository import VersionedEntity
from lib.automations.transitions import (ExecutionFence, bind_fence, new_run, prepare_step,
                                         record_failure, record_output, request_cancel)
from tests.support.automation_fixtures import clock_definition, skill_definition, trigger

pytestmark = pytest.mark.unit
FENCE = ExecutionFence('automation-job', 'worker-a', 1)


def definition(value=None, version=1):
    prepared = prepare_definition(value or clock_definition())
    return VersionedEntity(prepared.definition['id'], version, json.dumps({
        'definition': prepared.definition, 'deleted': False,
        'compiled': {'digest': prepared.digest, 'stepOrder': prepared.ordered_step_ids,
                     'actions': prepared.action_contracts}}))


def run(value=None, kind='task.started'):
    created = new_run(definition(value), trigger(kind), now=100)
    return bind_fence(created, FENCE, now=101)


def test_occurrence_identity_survives_rule_edits_and_snapshot_is_frozen():
    first = new_run(definition(), trigger(), now=100)
    changed = clock_definition()
    changed['name'] = 'edited while the event is redelivered'
    second = new_run(definition(changed, 2), trigger(), now=105)
    assert first['id'] == second['id']
    assert first['definitionRevision'] == 1
    assert first['snapshot']['name'] == 'Show task start time'
    assert second['definitionRevision'] == 2


def test_pure_rule_settles_only_after_typed_output_is_recorded():
    state, step_id = prepare_step(run(), FENCE, now=102)
    assert step_id == 'show'
    assert state['steps']['show']['status'] == 'dispatching'
    key = state['steps']['show']['effectKey']
    state = record_output(state, 'show', {'startedAt': 100, 'timezone': 'Asia/Shanghai',
                                        'text': '2026-10-03 13:00:00'}, FENCE, now=103)
    state, step_id = prepare_step(state, FENCE, now=104)
    assert state['status'] == 'succeeded'
    assert step_id is None
    assert state['steps']['show']['effectKey'] == key


def test_missing_or_wrong_output_cannot_settle_a_step():
    state, _ = prepare_step(run(), FENCE, now=102)
    with pytest.raises(AutomationError) as caught:
        record_output(state, 'show', {'startedAt': False}, FENCE, now=103)
    assert caught.value.code == 'invalid_action_output'
    assert state['steps']['show']['status'] == 'dispatching'


def test_lost_reader_claim_replays_with_stable_effect_identity():
    state, _ = prepare_step(run(), FENCE, now=102)
    key = state['steps']['show']['effectKey']
    new_fence = ExecutionFence('automation-job', 'worker-b', 2)
    recovered = bind_fence(state, new_fence, now=103)
    recovered, step_id = prepare_step(recovered, new_fence, now=104)
    assert step_id == 'show'
    assert recovered['steps']['show']['effectKey'] == key
    assert recovered['steps']['show']['attempts'] == 2
    with pytest.raises(AutomationConflict):
        record_output(recovered, 'show', {}, FENCE, now=105)


def advance_to_activation():
    state, _ = prepare_step(run(skill_definition(), 'schedule'), FENCE, now=102)
    check = {'changed': True, 'baselineEstablished': False, 'snapshotId': 'verified-v2',
             'revisions': [{'skillId': 'citadel', 'version': '2', 'digest': 'a' * 64, 'sourceId': 'source-1'}]}
    state = record_output(state, 'check', check, FENCE, now=103)
    return prepare_step(state, FENCE, now=104)[0]


def test_lost_write_claim_requires_reconciliation_and_never_replays():
    state = advance_to_activation()
    assert state['steps']['activate']['status'] == 'dispatching'
    recovered = bind_fence(state, ExecutionFence('automation-job', 'worker-b', 2), now=105)
    assert recovered['status'] == 'reconciliation_required'
    recovered, step_id = prepare_step(recovered, ExecutionFence('automation-job', 'worker-b', 2), now=106)
    assert step_id is None
    assert recovered['steps']['activate']['attempts'] == 1


def test_no_change_skips_agent_and_verification_without_a_conversation():
    state, _ = prepare_step(run(skill_definition(), 'schedule'), FENCE, now=102)
    state = record_output(state, 'check', {
        'changed': False, 'baselineEstablished': False, 'snapshotId': 'verified-v1',
        'revisions': [{'skillId': 'citadel', 'version': '1', 'digest': 'a' * 64, 'sourceId': 'source-1'}]},
        FENCE, now=103)
    state, step_id = prepare_step(state, FENCE, now=104)
    assert state['status'] == 'succeeded'
    assert step_id is None
    assert all(state['steps'][key]['status'] == 'skipped' for key in ('activate', 'improve', 'verify'))
    assert all('output' not in state['steps'][key] for key in ('activate', 'improve', 'verify'))


def advance_to_agent():
    state = advance_to_activation()
    state = record_output(state, 'activate', {
        'snapshotId': 'verified-v2', 'activated': True,
        'revisions': [{'skillId': 'citadel', 'version': '2', 'digest': 'a' * 64, 'sourceId': 'source-1'}]},
        FENCE, now=105)
    return prepare_step(state, FENCE, now=106)[0]


def agent_output(status='running'):
    return {'conversationId': 'auto-conv', 'taskId': 'task-1', 'turnId': 'turn-1',
            'attemptId': 'attempt-1', 'status': status, 'resultRef': 'artifact-1'}


def test_started_agent_is_waiting_and_not_workflow_success():
    state = record_output(advance_to_agent(), 'improve', agent_output(), FENCE, now=107)
    assert state['status'] == 'waiting_agent'
    assert state['steps']['improve']['status'] == 'waiting_agent'
    new_fence = ExecutionFence('automation-job', 'worker-b', 2)
    state = bind_fence(state, new_fence, now=108)
    assert state['status'] == 'waiting_agent'
    state, step_id = prepare_step(state, new_fence, now=109)
    assert step_id is None
    assert state['steps']['improve']['attempts'] == 1


def test_failed_verification_retains_evidence_and_does_not_report_success():
    state = record_output(advance_to_agent(), 'improve', agent_output('done'), FENCE, now=107)
    state, step_id = prepare_step(state, FENCE, now=108)
    assert step_id == 'verify'
    state = record_output(state, 'verify', {'passed': False,
        'checks': [{'id': 'citadel-mcp', 'passed': False, 'evidenceRef': 'test-failure-1'}]}, FENCE, now=109)
    assert state['status'] == 'failed'
    assert state['steps']['verify']['output']['checks'][0]['evidenceRef'] == 'test-failure-1'


def test_unknown_write_result_stays_pending_reconciliation_even_if_retryable():
    state = record_failure(advance_to_activation(), 'activate',
                           {'code': 'timeout', 'message': 'Result unknown'}, FENCE,
                           now=105, uncertain=True, retryable=True)
    assert state['status'] == 'reconciliation_required'
    assert state['steps']['activate']['attempts'] == 1


def test_safe_read_retries_are_bounded():
    state, _ = prepare_step(run(), FENCE, now=102)
    for attempt in range(3):
        state = record_failure(state, 'show', {'code': 'unavailable', 'message': 'Retry'},
                               FENCE, now=102 + attempt, retryable=True)
        if attempt < 2:
            state, step_id = prepare_step(state, FENCE, now=102 + attempt)
            assert step_id == 'show'
    assert state['status'] == 'failed'
    assert state['steps']['show']['attempts'] == 3


def test_cancelled_queued_run_does_not_start_and_active_agent_requires_settlement():
    queued = new_run(definition(), trigger(), now=100)
    cancelled = request_cancel(queued, reason='user_cancelled', now=101)
    assert cancelled['status'] == 'cancelled'
    with pytest.raises(AutomationConflict):
        bind_fence(cancelled, FENCE, now=102)
    active = record_output(advance_to_agent(), 'improve', agent_output(), FENCE, now=107)
    cancelling = request_cancel(active, reason='user_cancelled', now=108)
    assert cancelling['status'] == 'cancelling'
    settled = record_output(cancelling, 'improve', agent_output('cancelled'), FENCE, now=109)
    assert settled['status'] == 'cancelled'
    assert settled['steps']['verify']['status'] == 'cancelled'


def test_expired_run_does_not_dispatch_a_new_effect():
    state, step_id = prepare_step(run(), FENCE, now=111)
    assert step_id is None
    assert state['status'] == 'cancelled'
    assert state['cancelReason'] == 'deadline_exceeded'


def test_recursive_events_and_scope_mismatch_fail_closed():
    event = trigger()
    event['automationChain'] = ['show-start-time']
    with pytest.raises(AutomationError) as caught:
        new_run(definition(), event, now=100)
    assert caught.value.code == 'recursive_trigger'
    value = clock_definition()
    value['trigger']['scope'] = 'human'
    event = trigger()
    event['origin'] = 'automation'
    with pytest.raises(AutomationError) as caught:
        new_run(definition(value), event, now=100)
    assert caught.value.code == 'trigger_scope_mismatch'


def test_fence_identity_cannot_be_reused_by_another_worker():
    with pytest.raises(AutomationConflict):
        bind_fence(run(), ExecutionFence('automation-job', 'worker-b', 1), now=102)
    with pytest.raises(AutomationConflict):
        bind_fence(run(), ExecutionFence('another-job', 'worker-b', 2), now=102)


def test_waiting_permission_also_observes_the_wall_clock_deadline():
    state = run()
    state['status'] = 'waiting_permission'
    state['steps']['show']['status'] = 'waiting_permission'
    state, step_id = prepare_step(state, FENCE, now=111)
    assert step_id is None
    assert state['status'] == 'cancelled'
    assert state['cancelReason'] == 'deadline_exceeded'


@pytest.mark.parametrize('external_write', [False, True])
def test_cancelled_dispatch_recovery_keeps_cleanup_authority(external_write):
    state = advance_to_activation() if external_write else prepare_step(run(), FENCE, now=102)[0]
    state = request_cancel(state, reason='owner stop', now=105)
    state['cancelCommand'] = {'id':'first','reason':'owner stop','requestedAt':105}
    new_fence = ExecutionFence('automation-job', 'worker-b', 2)
    recovered = bind_fence(state, new_fence, now=106)
    assert recovered['status'] == 'cancelling'
    step = 'activate' if external_write else 'show'
    assert recovered['steps'][step]['status'] == 'reconciliation_required'
    unchanged, dispatch = prepare_step(recovered, new_fence, now=107)
    assert dispatch is None
    assert unchanged['cancelCommand'] == state['cancelCommand']
    settled = record_failure(recovered, step, {'code':'settled','message':'Effect settled'}, new_fence, now=108)
    assert settled['status'] == 'cancelled'


def test_failed_verification_during_cancel_preserves_evidence_and_cancel_outcome():
    state = record_output(advance_to_agent(), 'improve', agent_output('done'), FENCE, now=107)
    state, step = prepare_step(state, FENCE, now=108)
    state = request_cancel(state, reason='owner stop', now=109)
    result = record_output(state, step, {'passed':False, 'checks':[
        {'id':'citadel-mcp','passed':False,'evidenceRef':'failure'}]}, FENCE, now=110)
    assert result['status'] == 'cancelled'
    assert result['steps'][step]['error']['code'] == 'verification_failed'
    assert result['steps'][step]['output']['checks'][0]['evidenceRef'] == 'failure'

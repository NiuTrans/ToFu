"""Message submission pays for outcome, not repeated input/receipt bookkeeping."""
import json

import pytest

from lib.tasks_pkg.compaction._budget import budget_tool_result_v2
from lib.tools.result_envelope import split_tool_result_delivery
from lib.tools.result_surfaces import project_model_source

pytestmark = pytest.mark.unit


def receipt(**updates):
    return {'agent_id': 'dispatch-frontend', 'swarm_run_id': 'owned-run',
            'message_id': 'durable-message', 'state': 'submitted', 'kind': 'steer',
            'contentChars': 541, 'execution_started': False, **updates}


def test_model_gets_only_honest_submission_outcome():
    raw = json.dumps(receipt())
    view = project_model_source('send_agent_message', raw)
    assert view == 'Message sent successfully. Input consumption is not yet confirmed.'
    assert len(view) < len(raw) / 2
    assert 'dispatch-frontend' not in view and 'durable-message' not in view
    assert project_model_source('custom_tool', raw) == raw


@pytest.mark.parametrize(('state', 'expected'), [
    ('input_confirmed', 'does not prove acceptance or completion'),
    ('unconfirmed', 'run ended without confirmed input consumption'),
])
def test_replayed_outcomes_never_promote_submission_to_completion(state, expected):
    assert expected in project_model_source('send_agent_message', json.dumps(receipt(state=state)))


@pytest.mark.parametrize('value', [
    {'error': 'No owned current run matches this exact handle', 'code': 'message_run_not_found',
     'next_action': 'Read the exact scoped mailbox before retrying.'},
    receipt(next_action='Inspect mailbox before retrying.'),
    receipt(warning='Submission status is uncertain'),
    receipt(state='unknown'), receipt(execution_started=True), receipt(contentChars=True),
    receipt(kind='other'), receipt(agent_id=''), receipt(state=[]), receipt(contentChars=0),
    receipt(agent_id=123), receipt(contentChars='541'), receipt(contentChars=1.5),
    receipt(state='turn_started'),
    {'status': 'error', **receipt()},
    {'state': 'submitted'},
])
def test_failures_uncertainty_unknown_fields_and_invalid_receipts_pass_through(value):
    raw = json.dumps(value)
    assert project_model_source('send_agent_message', raw) == raw


@pytest.mark.parametrize('raw', ['Error: storage timeout; reuse the same idempotency key.',
                                   '{malformed', 'null', '[]'])
def test_non_receipt_text_is_unchanged(raw):
    assert project_model_source('send_agent_message', raw) == raw


def test_duplicate_fields_cannot_turn_uncertain_submission_into_success():
    raw = json.dumps(receipt())[:-1] + ', "state": "unconfirmed"}'
    assert project_model_source('send_agent_message', raw) == raw


def test_budget_retains_original_receipt_without_feeding_it_to_model(monkeypatch):
    raw = json.dumps(receipt())
    saved = []

    def persist(canonical, *, user_id):
        saved.append((user_id, canonical.text()))
        return 'tool-result:' + 'a' * 64

    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2('send_agent_message', raw, user_id=42)
    delivery = split_tool_result_delivery(envelope)
    assert delivery.model_text == project_model_source('send_agent_message', raw)
    assert saved == [(42, raw)]
    assert json.loads(envelope.canonical.text()) == receipt()
    assert delivery.evidence['retentionCoverage'] == 'complete'
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']


def stable_receipt(state='turn_started', **updates):
    assignment = {'agent_id': 'ordinary-child', 'swarm_run_id': 'new-owned-run',
                  'conversation_id': 'child-dialogue'}
    if state == 'turn_started':
        assignment['launch'] = {
            'status': 'async_launched', 'swarm_key': 'ordinary-swarm',
            'agents': [{'id': 'ordinary-child', 'conversation_id': 'child-dialogue',
                        'task_id': 'execution-task', 'swarm_run_id': 'new-owned-run'}],
        }
    return {'retained_agent_id': 'persistent-colleague', 'scope': 'project',
            'message_id': 'stable-durable-message', 'state': state, 'kind': 'reference',
            'contentChars': 541, 'execution_started': state == 'turn_started',
            'assignment': assignment, **updates}


@pytest.mark.parametrize('state', ['turn_started', 'submitted', 'input_confirmed', 'unconfirmed'])
@pytest.mark.parametrize('scope', ['project', 'conversation'])
def test_stable_model_projection_retains_pinned_assignment_and_launch_handles(state, scope):
    value = stable_receipt(state, scope=scope)
    raw = json.dumps(value, ensure_ascii=False, indent=2)
    assert project_model_source('send_agent_message', raw) == raw
    view = json.loads(project_model_source('send_agent_message', raw))
    assert view == value
    assert view['assignment']['agent_id'] == 'ordinary-child'
    assert view['assignment']['swarm_run_id'] == 'new-owned-run'
    assert view['assignment']['conversation_id'] == 'child-dialogue'
    if state == 'turn_started':
        assert view['execution_started'] is True
        assert view['assignment']['launch']['agents'][0]['task_id'] == 'execution-task'


@pytest.mark.parametrize('state', ['turn_started', 'submitted'])
def test_stable_budget_delivers_actionable_handles_not_only_canonical_retention(monkeypatch, state):
    value = stable_receipt(state)
    raw = json.dumps(value)
    saved = []

    def persist(canonical, *, user_id):
        saved.append((user_id, canonical.text()))
        return 'tool-result:' + 'b' * 64

    monkeypatch.setattr('lib.tools.result_value.persist_result', persist)
    envelope = budget_tool_result_v2('send_agent_message', raw, user_id=42)
    delivery = split_tool_result_delivery(envelope)
    assert json.loads(delivery.model_text) == value
    # Unchanged complete JSON is retained inline; only the compacted legacy
    # view needs an artifact to retain fields omitted from model delivery.
    assert saved == []
    assert json.loads(envelope.canonical.text()) == value
    assert delivery.evidence['retentionCoverage'] == 'complete'
    assert delivery.evidence['viewCoverage'] == 'complete'
    assert not delivery.evidence['truncated']


@pytest.mark.parametrize('submission_state', ['rejected', 'unknown'])
def test_typed_stable_errors_preserve_submission_evidence_and_raw_diagnostics(submission_state):
    value = {'status': 'error', 'submission_state': submission_state,
             'code': 'retained_agent_activation_unknown', 'message': 'Raw diagnostic <unchanged>',
             'next_action': 'Read the same stable key before retrying.',
             'receipt': stable_receipt('unconfirmed')}
    raw = json.dumps(value)
    assert project_model_source('send_agent_message', raw) == raw

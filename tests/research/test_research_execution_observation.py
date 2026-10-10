"""Remote/background execution facts survive complete-result projection loss."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from lib.research.action import capture_research_result, _tool_receipts, apply_action_result
from lib.research.program import normalize_run_evidence
from lib.research.workspace import empty_workspace
from lib.research.execution_observation import extract_execution_observation
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def capture(payload, tool='remote-job'):
    row = {'toolName': tool, 'status': 'done', 'toolCallId': 'call'}
    capture_research_result(row, json.dumps({'large_log': 'x' * 20000, **payload}))
    row['toolContent'] = 'presentation summary'
    return _tool_receipts([row])[0]


@pytest.mark.parametrize('state,expected', [('planned', 'planned'), ('queued', 'queued'), ('running', 'running'),
    ('completed', 'completed'), ('failed', 'failed')])
def test_remote_job_state_and_identity_beyond_excerpt_are_preserved(state, expected):
    receipt = capture({'job': {'id': 'job-precise', 'status': state}})
    assert receipt['execution_observation'] == {'state': expected, 'job_id': 'job-precise'}
    assert 'job-precise' not in receipt['result_excerpt']
    workspace = apply_action_result(action='experiment', workspace=empty_workspace('Remote jobs'),
        task_id='task', receipts=[receipt], bindings=[{'tool': 'remote-job', 'capability': 'compute.status'}],
        payload={'run': {'id': 'run', 'remote_job_id': 'job-precise'}})
    assert workspace['runs'][0]['execution_status'] == expected
    assert workspace['runs'][0]['remote_job_id'] == 'job-precise'
    normalized = normalize_run_evidence(workspace['runs'])[0]['tool_receipts'][0]
    assert normalized['execution_observation'] == {**receipt['execution_observation'],
        'provider_scope': 'tool:remote-job'}
    assert 'provider_scope' not in receipt['execution_observation']


def test_background_pending_is_not_rewritten_as_failure_by_normalization():
    row = {'toolName': 'run_command', 'status': 'done'}
    capture_research_result(row, '$ long-experiment\n[still running]')
    receipt = _tool_receipts([row])[0]
    assert receipt['status'] == 'pending' and receipt['execution_observation']['state'] == 'planned'
    normalized = normalize_run_evidence([{'id': 'run', 'tool_receipts': [receipt]}])[0]
    assert normalized['tool_receipts'][0]['status'] == 'pending'
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/research_program_v1.schema.json').read_text())
    Draft202012Validator({'$defs': schema['$defs'], '$ref': '#/$defs/toolReceipt'}).validate(normalized['tool_receipts'][0])


def test_command_exit_does_not_complete_an_async_job_in_its_body():
    row = {'toolName': 'run_command', 'status': 'done', 'toolArgs': json.dumps({'command': 'submit'})}
    capture_research_result(row, '$ submit\n' + json.dumps({'log': 'x'*10000,
        'job': {'id': 'active', 'state': 'running'}}) + '\n[exit code: 0]')
    receipt = _tool_receipts([row])[0]
    assert receipt['execution_observation'] == {'state': 'running', 'job_id': 'active'}
    from lib.research.execution_observation import receipt_execution_status
    assert receipt_execution_status(receipt, remote=False) == 'running'


def test_generic_result_id_is_not_invented_as_job_identity():
    assert extract_execution_observation({'result': {'id': 'artifact', 'status': 'completed'}}) == {'state': 'completed'}
    assert extract_execution_observation({'status': 'success', 'id': 'api-request'}) is None
    assert extract_execution_observation({'job': {'id': 'x'*241, 'status': 'running'}}) == {'state': 'running'}


@pytest.mark.parametrize('observation', [{'state': 'success'}, {'state': 'running', 'job_id': 'x'*241},
    {'state': 'running', 'invented': True}])
def test_malformed_execution_metadata_is_refused(observation):
    receipt = capture({'status': 'running'}); receipt['execution_observation'] = observation
    with pytest.raises(ValueError): normalize_run_evidence([{'id': 'run', 'tool_receipts': [receipt]}])


def test_execution_observation_survives_selected_storage_restart(storage):
    supervisor, _ = storage
    from lib.research.workspace import save_workspace, load_workspace
    workspace = empty_workspace('Long remote execution')
    receipt = capture({'job': {'id': 'durable-job', 'status': 'running'}})
    workspace['runs'] = [{'id': 'run', 'execution_status': 'running', 'tool_receipts': [receipt]}]
    saved = save_workspace('Long remote execution', 'en', workspace, expected_revision=0, user_id=1)
    supervisor.stop(); supervisor.start()
    restored = load_workspace('Long remote execution', 'en', user_id=1)
    assert restored['runs'][0]['tool_receipts'][0]['execution_observation'] == receipt['execution_observation']
    assert saved['runs'] == restored['runs']


@pytest.mark.parametrize('completed_job,provider,expected', [
    ('different-job', 'cluster', 'running'), ('active-job', 'cluster', 'completed'),
    ('active-job', 'another-cluster', 'running')])
def test_only_same_job_in_same_provider_can_clear_observed_running_work(completed_job, provider, expected):
    first = capture({'job': {'id': 'active-job', 'status': 'running'}}, tool='submit')
    last = capture({'job': {'id': completed_job, 'status': 'completed'}}, tool='poll')
    workspace = apply_action_result(action='experiment', workspace=empty_workspace('Remote jobs'),
        task_id='task', receipts=[first, last], bindings=[
            {'tool': 'submit', 'capability': 'compute.submit', 'provider': 'cluster'},
            {'tool': 'poll', 'capability': 'compute.status', 'provider': provider}],
        payload={'run': {'id': 'run', 'status': 'passed'}})
    assert workspace['runs'][0]['execution_status'] == expected
    if expected != 'completed': assert not workspace['runs'][0]['artifact_refs']


def test_failed_poll_cannot_clear_a_previously_observed_running_job():
    first = capture({'job': {'id': 'active-job', 'status': 'running'}}, tool='submit')
    failed_poll = capture({'ok': False, 'error': 'transport failure',
        'job': {'id': 'active-job', 'status': 'completed'}}, tool='poll')
    workspace = apply_action_result(action='experiment', workspace=empty_workspace('Remote jobs'),
        task_id='task', receipts=[first, failed_poll], bindings=[
            {'tool': 'submit', 'capability': 'compute.submit', 'provider': 'cluster'},
            {'tool': 'poll', 'capability': 'compute.status', 'provider': 'cluster'}],
        payload={'run': {'id': 'run', 'status': 'passed'}})
    assert workspace['runs'][0]['execution_status'] == 'running'
    assert not workspace['runs'][0]['artifact_refs']


@pytest.mark.parametrize('payload', [
    {'job_id': 'accepted-job', 'ok': True},
    {'job': {'id': 'accepted-job'}, 'status': 'completed'},
    {'job': {'id': 'accepted-job', 'status': 'success'}, 'status': 'completed'},
])
def test_successful_submit_command_with_unsettled_job_handle_stays_planned(payload):
    row = {'toolName': 'run_command', 'status': 'done', 'toolArgs': json.dumps({'command': 'submit'})}
    capture_research_result(row, '$ submit\n' + json.dumps(payload) + '\n[exit code: 0]')
    receipt = _tool_receipts([row])[0]
    assert receipt['execution_observation'] == {'state': 'planned', 'job_id': 'accepted-job'}
    workspace = apply_action_result(action='experiment', workspace=empty_workspace('Submission'),
        task_id='submit-task', receipts=[receipt], bindings=[],
        payload={'run': {'id': 'run', 'status': 'passed', 'remote_job_id': 'accepted-job'}})
    assert workspace['runs'][0]['execution_status'] == 'planned'
    assert not workspace['runs'][0]['artifact_refs']


def test_unsettled_named_job_overrides_generic_api_completion_without_inventing_id():
    assert extract_execution_observation({'job': {}, 'status': 'completed'}) == {'state': 'planned'}
    assert extract_execution_observation({'job': {'id': 'x'*241}, 'status': 'completed'}) == {'state': 'planned'}
    assert extract_execution_observation({'result': {'id': 'artifact'}, 'status': 'completed'}) == {'state': 'completed'}


def test_observed_submit_handle_can_settle_through_matching_later_status():
    submitted = capture({'job_id': 'accepted-job', 'ok': True}, tool='submit')
    polled = capture({'job': {'id': 'accepted-job', 'status': 'completed'}}, tool='poll')
    workspace = apply_action_result(action='experiment', workspace=empty_workspace('Submission'),
        task_id='task', receipts=[submitted, polled], bindings=[
            {'tool': 'submit', 'capability': 'compute.submit', 'provider': 'cluster'},
            {'tool': 'poll', 'capability': 'compute.status', 'provider': 'cluster'}],
        payload={'run': {'id': 'run'}})
    assert workspace['runs'][0]['execution_status'] == 'completed'

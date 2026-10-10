"""Cross-action polls retain unfinished job facts and original receipt identity."""
from copy import deepcopy

import pytest

from lib.research.action import apply_action_result
from lib.research.workspace import empty_workspace
from tests.research.test_research_execution_observation import capture
from tests.research.test_research_workspace_evidence_guard import storage as storage

pytestmark = pytest.mark.unit


def receipt(job, state, tool, call):
    result = capture({'job': {'id': job, 'status': state}}, tool=tool)
    result['call_id'] = call
    return result


def update(workspace, task, receipts, *, provider='cluster', tool='poll'):
    return apply_action_result(action='experiment', workspace=workspace, task_id=task,
        receipts=receipts, payload={'run': {'id': 'run'}}, bindings=[
            {'tool': tool, 'capability': 'compute.status', 'provider': provider}])


def initial():
    return update(empty_workspace('Long jobs'), 'submit-task', [
        receipt('job-A', 'running', 'submit', 'call-A'),
        receipt('job-B', 'completed', 'submit', 'call-B')], tool='submit')


def test_later_poll_cannot_drop_other_running_job_or_old_receipts():
    first = initial(); retained = deepcopy(first['runs'][0]['tool_receipts'])
    second = update(first, 'poll-B', [receipt('job-B', 'completed', 'poll', 'call-B')])
    run = second['runs'][0]
    assert run['execution_status'] == 'running' and not run['artifact_refs']
    assert run['tool_receipts'][:2] == retained
    assert [row['source_task_id'] for row in run['tool_receipts']] == ['submit-task', 'submit-task', 'poll-B']
    final = update(second, 'poll-A', [receipt('job-A', 'completed', 'poll', 'call-A')])
    assert final['runs'][0]['execution_status'] == 'completed'
    assert len(final['runs'][0]['tool_receipts']) == 4
    assert first['runs'][0]['tool_receipts'] == retained


def test_changed_current_provider_cannot_reinterpret_old_job_scope():
    first = initial()
    second = update(first, 'new-cluster-poll', [receipt('job-A', 'completed', 'poll', 'call')], provider='another')
    assert second['runs'][0]['execution_status'] == 'running'
    assert second['runs'][0]['tool_receipts'][0]['execution_observation']['provider_scope'] == 'provider:cluster'


def test_same_origin_reapplication_is_idempotent_but_other_task_is_distinct():
    first = initial(); observation = receipt('job-B', 'completed', 'poll', 'call')
    second = update(first, 'poll-task', [observation])
    replay = deepcopy(observation); replay['observed_at'] += 100
    third = update(second, 'poll-task', [replay])
    assert third['runs'][0]['tool_receipts'] == second['runs'][0]['tool_receipts']
    independent = update(third, 'another-task', [observation])
    assert len(independent['runs'][0]['tool_receipts']) == 4


def test_changed_same_origin_receipt_cannot_overwrite_or_append_a_second_fact():
    first = initial(); observation = receipt('job-B', 'completed', 'poll', 'call')
    second = update(first, 'poll-task', [observation]); before = deepcopy(second)
    changed = deepcopy(observation); changed['result_digest'] = 'different'
    with pytest.raises(ValueError, match='source identity conflicts'): update(second, 'poll-task', [changed])
    assert second == before


@pytest.mark.parametrize('field,value', [('hypothesis', 'A different scientific hypothesis'),
    ('protocol', {'primary_metric': 'another metric'})])
def test_scientific_basis_change_cannot_rebind_pending_run(field, value):
    first = initial(); first[field] = value
    before = deepcopy(first)
    with pytest.raises(ValueError, match='different scientific basis'):
        update(first, 'poll', [receipt('job-A', 'completed', 'poll', 'call')])
    assert first == before


def test_overflow_keeps_original_pending_receipts_without_truncation():
    first = initial(); run = first['runs'][0]
    run['tool_receipts'] = [dict(deepcopy(run['tool_receipts'][0]), call_id=f'old-{i}') for i in range(64)]
    before = deepcopy(first)
    with pytest.raises(ValueError, match='overflow'):
        update(first, 'poll', [receipt('job-A', 'completed', 'poll', 'new')])
    assert first == before


def test_pending_job_lineage_survives_selected_storage_restart(storage):
    supervisor, _ = storage
    from lib.research.workspace import save_workspace, load_workspace
    saved = save_workspace('Long jobs', 'en', initial(), expected_revision=0, user_id=1)
    supervisor.stop(); supervisor.start()
    restored = load_workspace('Long jobs', 'en', user_id=1)
    assert restored['runs'][0]['tool_receipts'] == saved['runs'][0]['tool_receipts']
    polled = update(restored, 'poll-B', [receipt('job-B', 'completed', 'poll', 'call')])
    assert polled['runs'][0]['execution_status'] == 'running'
    saved_poll = save_workspace('Long jobs', 'en', polled, expected_revision=restored['revision'], user_id=1)
    assert len(saved_poll['runs'][0]['tool_receipts']) == 3
    assert saved_poll['runs'][0]['tool_receipts'][0]['source_task_id'] == 'submit-task'


def test_already_captured_provider_scope_cannot_be_rebound():
    observation = receipt('job-A', 'completed', 'poll', 'call')
    observation['execution_observation']['provider_scope'] = 'provider:original'
    with pytest.raises(ValueError, match='cannot replace its provider scope'):
        update(empty_workspace('Long jobs'), 'poll', [observation], provider='different')


def test_pending_lineage_conforms_to_program_wire_contract():
    import json
    from pathlib import Path
    from jsonschema import Draft202012Validator
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/research_program_v1.schema.json').read_text())
    Draft202012Validator(schema).validate(initial())


@pytest.mark.parametrize('failed_state', ['failed', 'cancelled', 'rejected'])
def test_completed_other_job_cannot_hide_failed_job(failed_state):
    first = initial()
    final = update(first, 'terminal-polls', [
        receipt('job-A', failed_state, 'poll', 'failed-A'),
        receipt('job-B', 'completed', 'poll', 'completed-B')])
    run = final['runs'][0]
    assert run['execution_status'] == 'failed'
    assert run['scientific_outcome'] == 'inconclusive'
    assert not run['artifact_refs']
    assert len(run['tool_receipts']) == 4


def test_active_job_remains_pending_while_another_job_has_failed():
    first = initial()
    pending = update(first, 'failed-B', [receipt('job-B', 'failed', 'poll', 'failed-B')])
    assert pending['runs'][0]['execution_status'] == 'running'
    final = update(pending, 'completed-A', [receipt('job-A', 'completed', 'poll', 'completed-A')])
    assert final['runs'][0]['execution_status'] == 'failed'


def test_failed_job_scope_survives_selected_storage_restart(storage):
    supervisor, _ = storage
    from lib.research.workspace import save_workspace, load_workspace
    pending = update(initial(), 'failed-B', [receipt('job-B', 'failed', 'poll', 'failed-B')])
    saved = save_workspace('Long jobs', 'en', pending, expected_revision=0, user_id=1)
    supervisor.stop(); supervisor.start()
    restored = load_workspace('Long jobs', 'en', user_id=1)
    final = update(restored, 'completed-A', [receipt('job-A', 'completed', 'poll', 'completed-A')])
    final = save_workspace('Long jobs', 'en', final, expected_revision=saved['revision'], user_id=1)
    assert final['runs'][0]['execution_status'] == 'failed'
    assert len(final['runs'][0]['tool_receipts']) == 4


def test_another_provider_failed_job_does_not_replace_same_named_job():
    pending = update(initial(), 'failed-other-A', [receipt('job-A', 'failed', 'poll', 'failed')], provider='another')
    assert pending['runs'][0]['execution_status'] == 'running'
    final = update(pending, 'complete-A', [receipt('job-A', 'completed', 'poll', 'completed')])
    assert final['runs'][0]['execution_status'] == 'failed'

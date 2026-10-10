"""Settled multi-action observations consume each explicit source only once."""
from copy import deepcopy

import pytest

from tests.research.test_research_pending_lineage import initial, receipt, update

pytestmark = pytest.mark.unit


def completed():
    observation = receipt('job-A', 'completed', 'poll', 'call-A')
    workspace = update(initial(), 'poll-A', [observation])
    assert workspace['runs'][0]['execution_status'] == 'completed'
    return workspace, observation


def test_replaying_settling_poll_does_not_create_another_completed_run():
    workspace, observation = completed()
    replay = update(workspace, 'poll-A', [observation])
    assert len(replay['runs']) == 1
    assert replay['runs'][0] == workspace['runs'][0]


def test_replaying_earlier_submission_cannot_regress_settled_run():
    workspace, _ = completed()
    replay = update(workspace, 'submit-task', [
        receipt('job-A', 'running', 'submit', 'call-A'),
        receipt('job-B', 'completed', 'submit', 'call-B')], tool='submit')
    assert replay['runs'] == workspace['runs']


def test_replay_ignores_changed_timestamp_and_preserves_original_input():
    workspace, observation = completed(); before = deepcopy(workspace)
    observation['observed_at'] += 1000
    replay = update(workspace, 'poll-A', [observation])
    assert replay['runs'] == before['runs'] and workspace == before


def test_consumed_source_conflict_is_refused_after_settlement():
    workspace, observation = completed(); before = deepcopy(workspace)
    observation['result_digest'] = 'different'
    with pytest.raises(ValueError, match='source identity conflicts'):
        update(workspace, 'poll-A', [observation])
    assert workspace == before


def test_a_distinct_source_is_not_discarded_as_replay():
    workspace, observation = completed()
    independent = update(workspace, 'another-task', [observation])
    assert len(independent['runs']) == 2
    assert independent['runs'][0] == workspace['runs'][0]


def test_replay_does_not_bind_old_execution_to_new_hypothesis():
    workspace, observation = completed(); original = deepcopy(workspace['runs'][0])
    workspace['hypothesis'] = 'A changed scientific question'
    replay = update(workspace, 'poll-A', [observation])
    assert len(replay['runs']) == 1
    assert replay['runs'][0]['hypothesis_digest'] == original['hypothesis_digest']
    assert replay['runs'][0]['task_id'] == original['task_id']


def test_replayed_source_cannot_introduce_new_supported_claims():
    from lib.research.action import apply_action_result
    workspace, observation = completed()
    replay = apply_action_result(action='experiment', workspace=workspace, task_id='poll-A',
        receipts=[observation], bindings=[{'tool': 'poll', 'capability': 'compute.status', 'provider': 'cluster'}],
        payload={'run': {'id': 'run'}, 'claims': [{'id': 'invented', 'text': 'New unsupported conclusion',
                                               'status': 'supported', 'evidence_refs': ['run']}]})
    assert replay['claims'] == workspace['claims']


def test_model_renaming_cannot_make_a_consumed_source_novel():
    from lib.research.action import apply_action_result
    workspace, observation = completed()
    replay = apply_action_result(action='experiment', workspace=workspace, task_id='poll-A',
        receipts=[observation], bindings=[{'tool': 'poll', 'capability': 'compute.status', 'provider': 'cluster'}],
        payload={'run': {'id': 'renamed-study'}})
    assert replay['runs'] == workspace['runs']


from tests.research.test_research_workspace_evidence_guard import storage as storage


def test_completed_replay_stays_consumed_after_selected_storage_restart(storage):
    supervisor, _ = storage
    from lib.research.workspace import save_workspace, load_workspace
    workspace, observation = completed()
    saved = save_workspace('Long jobs', 'en', workspace, expected_revision=0, user_id=1)
    supervisor.stop(); supervisor.start()
    restored = load_workspace('Long jobs', 'en', user_id=1)
    replay = update(restored, 'poll-A', [observation])
    persisted = save_workspace('Long jobs', 'en', replay, expected_revision=restored['revision'], user_id=1)
    assert len(persisted['runs']) == 1
    assert persisted['runs'][0] == saved['runs'][0]

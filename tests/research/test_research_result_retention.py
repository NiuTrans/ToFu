"""Completed scientific observations survive model ID reuse and replay."""
from copy import deepcopy
import json

import pytest

from lib.research.action import apply_action_result
from lib.research.program import MAX_RUNS, normalize_program_fields, readiness
from lib.research.workspace import empty_workspace

pytestmark = pytest.mark.unit


def observe(workspace, task_id, outcome, *, run_id='r1', observed_at=0):
    return apply_action_result(
        action='experiment', workspace=workspace, task_id=task_id, bindings=[],
        payload={'run': {'id': run_id, 'status': 'passed'}, 'claims': [{
            'id': 'c1', 'text': 'Scoped observation', 'status': 'supported',
            'outcome_scope': outcome, 'evidence_refs': [run_id] if run_id else [task_id]}]},
        receipts=[{'tool': 'run_command', 'status': 'done',
                   'artifact_ref': 'evidence:' + task_id + ':' + outcome,
                   'result_digest': outcome, 'observed_at': observed_at,
                   'result_excerpt': json.dumps({'scientific_outcome': outcome})}])


@pytest.mark.parametrize('first_outcome', ['supported', 'refuted'])
def test_reused_model_id_retains_opposing_completed_observations(first_outcome):
    first = observe(empty_workspace('Scope'), 'task-1', first_outcome)
    original = deepcopy(first['runs'][0])
    opposite = 'refuted' if first_outcome == 'supported' else 'supported'
    second = observe(first, 'task-2', opposite)
    assert len(second['runs']) == 2
    assert second['runs'][0] == original
    current = second['runs'][1]
    assert current['id'] != original['id']
    assert current['task_id'] == 'task-2'
    assert second['claims'][0]['evidence_refs'] == [current['id']]
    assert second['claims'][0]['status'] == 'contested'
    assert second['decision']['status'] == 'refine'


def test_identical_reapplication_reuses_retained_identity_despite_timestamps():
    first = observe(empty_workspace('Scope'), 'task-1', 'refuted')
    second = observe(first, 'task-2', 'supported', observed_at=1)
    retained_id = second['runs'][1]['id']
    second['runs'][1]['updated_at'] = 1
    original = deepcopy(second['runs'][1])
    replay = observe(second, 'task-2', 'supported', observed_at=99)
    assert len(replay['runs']) == 2
    assert replay['runs'][1]['id'] == retained_id
    assert replay['runs'][1] == original
    assert replay['decision']['status'] == 'refine'


def test_changed_result_from_same_task_does_not_erase_previous_evidence():
    first = observe(empty_workspace('Scope'), 'task-1', 'refuted')
    second = observe(first, 'task-1', 'supported')
    assert len(second['runs']) == 2
    assert second['runs'][0]['scientific_outcome'] == 'refuted'
    assert second['decision']['status'] == 'refine'


def test_missing_model_id_has_stable_execution_identity(monkeypatch):
    import lib.research.action as action
    monkeypatch.setattr(action.time, "time", lambda: 1000)
    first = observe(empty_workspace('Scope'), 'task-1', 'supported', run_id=None)
    monkeypatch.setattr(action.time, 'time', lambda: 2000)
    replay = observe(first, 'task-1', 'supported', run_id=None, observed_at=99)
    assert len(replay['runs']) == 1
    assert replay['runs'][0]['id'] == first['runs'][0]['id']
    assert replay['claims'][0]['status'] == 'supported'


def test_pending_observation_can_settle_through_existing_progression():
    pending = apply_action_result(action='experiment', workspace=empty_workspace('Scope'),
        payload={'run': {'id': 'r1'}}, receipts=[], task_id='submission', bindings=[])
    assert pending['runs'][0]['execution_status'] == 'planned'
    completed = observe(pending, 'status-check', 'supported')
    assert len(completed['runs']) == 1
    assert completed['runs'][0]['id'] == 'r1'
    assert completed['runs'][0]['execution_status'] == 'completed'


def full_workspace():
    workspace = observe(empty_workspace('Scope'), 'original', 'supported')
    original = workspace['runs'][0]
    workspace['runs'].extend(dict(deepcopy(original), id=f'retained-{i}')
                             for i in range(MAX_RUNS - 1))
    return workspace


def test_overflow_never_silently_discards_new_counterevidence_or_mutates_input():
    workspace = full_workspace()
    before = deepcopy(workspace)
    with pytest.raises(ValueError, match='no results were truncated'):
        observe(workspace, 'new-negative', 'refuted')
    assert workspace == before


def test_readiness_cannot_exclude_counterevidence_beyond_collection_boundary():
    workspace = full_workspace()
    opposing = dict(deepcopy(workspace['runs'][0]), id='counterevidence', scientific_outcome='refuted')
    workspace['runs'].append(opposing)
    with pytest.raises(ValueError, match='no results were truncated'):
        readiness(workspace)


def test_ambiguous_duplicate_run_ids_cannot_be_collapsed_by_later_merge():
    workspace = observe(empty_workspace('Scope'), 'task-1', 'supported')
    workspace['runs'].append(dict(deepcopy(workspace['runs'][0]), scientific_outcome='refuted'))
    with pytest.raises(ValueError, match='duplicate run identities'):
        normalize_program_fields(workspace)


def test_new_experiment_cannot_collapse_ambiguous_existing_identities():
    workspace = observe(empty_workspace('Scope'), 'task-1', 'supported')
    workspace['runs'].append(dict(deepcopy(workspace['runs'][0]), scientific_outcome='refuted'))
    before = deepcopy(workspace)
    with pytest.raises(ValueError, match='duplicate run identities'):
        observe(workspace, 'task-2', 'supported')
    assert workspace == before


@pytest.mark.parametrize('malformed', ['missing structured counterevidence', None])
def test_malformed_rows_are_not_silently_removed_from_the_reviewed_evidence(malformed):
    workspace = observe(empty_workspace('Scope'), 'task-1', 'supported')
    workspace['runs'].append(malformed)
    with pytest.raises(ValueError, match='malformed records'):
        readiness(workspace)


def test_reapplication_preserves_the_frozen_scientific_snapshot():
    from lib.research.program import scientific_dependency_digest
    first = observe(empty_workspace('Scope'), 'task-1', 'supported', observed_at=1)
    snapshot = scientific_dependency_digest(first)
    replay = observe(first, 'task-1', 'supported', observed_at=99)
    assert replay['runs'] == first['runs']
    assert scientific_dependency_digest(replay) == snapshot


def test_different_tails_of_long_results_retain_distinct_observations():
    from lib.research.action import _tool_receipts
    import hashlib
    prefix='measurement log\n' + 'x' * 5000
    workspace=empty_workspace('Long evidence')
    snapshots=[]
    for tail in ('metric=0.2','metric=0.8'):
        content=prefix+tail
        receipts=_tool_receipts([{'toolName':'run_command','status':'done','toolContent':content}])
        assert receipts[0]['result_digest']==hashlib.sha256(content.encode('utf-8')).hexdigest()
        assert len(receipts[0]['result_excerpt'])==4000
        workspace=apply_action_result(action='experiment',workspace=workspace,
            task_id='same-task',bindings=[],payload={'run':{'id':'same-run'}},receipts=receipts)
        snapshots.append(deepcopy(workspace['runs'][-1]))
    assert len(workspace['runs'])==2
    assert workspace['runs']==snapshots


@pytest.mark.parametrize('content',['', '测量🙂' * 40000], ids=['empty','large-unicode'])
def test_full_receipt_digest_matches_utf8_across_chunk_boundaries(content):
    from lib.research.action import _tool_receipts
    import hashlib
    receipt=_tool_receipts([{'status':'done','toolContent':content}])[0]
    assert receipt['result_digest']==hashlib.sha256(content.encode('utf-8')).hexdigest()
    assert receipt['result_excerpt']==content[:4000]

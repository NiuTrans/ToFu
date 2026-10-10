"""Manuscript currency depends on reviewed science, not compilation alone."""
import copy

import json

from lib.research.decisions import AUDIT_CHECKS
from tests.support.research_observations import observation_payload

import pytest

from tests.support.research_compilation import compilation_projection

from lib.research.action import apply_action_result
from lib.research.program import (
    claim_evidence_digest, dependency_digests, normalize_program_fields,
    readiness, scientific_dependency_digest, source_tree_digest,
)
from lib.research.workspace import empty_workspace

pytestmark = pytest.mark.unit


def _support(workspace):
    workspace['protocol']['random_seeds'] = [1,2,3]
    dependencies = dependency_digests(workspace)
    workspace['runs'] = [{
        'id': 'r1', 'status': 'passed', 'execution_status': 'completed',
        'scientific_outcome': 'supported', 'artifact_ref': 'evidence:1',
        'tool_receipts': [{'tool': 'run_command', 'status': 'done',
                           'result_digest': 'abc', 'result_excerpt': json.dumps(observation_payload(workspace['protocol'],'evidence:1'))}], **dependencies,
    }]
    workspace.update(normalize_program_fields(workspace))
    claim = {'id': 'c1', 'text': workspace['hypothesis'], 'status': 'supported',
             'evidence_refs': ['r1'], **dependencies}
    claim['evidence_digest'] = claim_evidence_digest(claim, workspace['runs'])
    workspace['claims'] = [claim]
    workspace.update(normalize_program_fields(workspace))


def _generate(workspace):
    return apply_action_result(
        action='manuscript', workspace=workspace, task_id='write1', bindings=[],
        receipts=[], payload={'manuscript': {
            key: 'Reviewed scientific text' for key in
            ('title', 'abstract', 'method', 'results', 'limitations')},
            'source_updates': [{'path': 'main.tex', 'content': 'Reviewed source'}]})


def _compiled(workspace):
    digest = source_tree_digest(workspace['source_files'])
    workspace['compilation'] = compilation_projection(workspace['source_files'])
    workspace['publication'].update(status='published', source_digest=digest)


def _ready_workspace():
    workspace = empty_workspace('Science')
    workspace['hypothesis'] = 'Initial hypothesis'
    workspace['protocol'].update({key: 'Specified' for key in
        ('primary_metric', 'baseline', 'dataset', 'falsifier')})
    _support(workspace)
    workspace = _generate(workspace)
    _compiled(workspace)
    assert readiness(workspace)['ready']
    assert readiness(workspace)['published_current']
    return workspace


def test_new_supported_science_does_not_revalidate_old_manuscript():
    workspace = _ready_workspace()
    old_digest = workspace['manuscript']['scientific_dependency_digest']
    workspace['hypothesis'] = 'New hypothesis with newly supported claim'
    _support(workspace)
    assert workspace['claims'][0]['status'] == 'supported'
    assert workspace['manuscript']['scientific_dependency_digest'] == old_digest
    got = readiness(workspace)
    assert not got['ready']
    assert not got['published_current']
    gates = {row['id']: row['ok'] for row in got['gates']}
    assert gates['compile'] and gates['claims'] and gates['run_evidence']
    assert not gates['manuscript']
    workspace = _generate(workspace)
    _compiled(workspace)
    assert readiness(workspace)['ready']
    assert readiness(workspace)['published_current']


def test_legacy_manuscript_remains_unverified_until_review():
    workspace = _ready_workspace()
    del workspace['manuscript']['scientific_dependency_digest']
    workspace.update(normalize_program_fields(workspace))
    assert workspace['manuscript']['scientific_dependency_digest'] == ''
    assert not readiness(workspace)['ready']
    assert not readiness(workspace)['published_current']
    unchanged = apply_action_result(action='manuscript', workspace=workspace,
        task_id='empty', bindings=[], receipts=[], payload={})
    assert not readiness(unchanged)['ready']
    regenerated = _generate(workspace)
    _compiled(regenerated)
    assert readiness(regenerated)['ready']


def test_scientific_digest_is_stable_under_normalization():
    workspace = _ready_workspace()
    digest = scientific_dependency_digest(workspace)
    for _ in range(3):
        workspace.update(normalize_program_fields(workspace))
        assert scientific_dependency_digest(workspace) == digest
        assert readiness(workspace)['ready']
    workspace['hypothesis'] = '  ' + workspace['hypothesis'] + '  '
    workspace['protocol']['baseline'] = '  Specified  '
    assert scientific_dependency_digest(workspace) == digest


@pytest.mark.parametrize('change', ['protocol', 'claim', 'evidence'])
def test_snapshot_covers_protocol_claims_and_supporting_evidence(change):
    workspace = _ready_workspace()
    changed = copy.deepcopy(workspace)
    if change == 'protocol':
        changed['protocol']['evaluation_protocol'] = 'Revised evaluation'
    elif change == 'claim':
        changed['claims'][0]['text'] = 'Different assertion'
    else:
        changed['runs'][0]['tool_receipts'][0]['result_digest'] = 'different'
    assert scientific_dependency_digest(changed) != scientific_dependency_digest(workspace)
    assert not readiness(changed)['published_current']


def test_new_opposing_result_invalidates_science_even_with_complete_audit_receipts():
    workspace = _ready_workspace()
    opposing = copy.deepcopy(workspace['runs'][0])
    opposing.update(id='r2', scientific_outcome='refuted')
    opposing['tool_receipts'][0]['result_digest'] = 'verified-refutation'
    workspace['runs'].append(opposing)
    got = readiness(workspace)
    assert not got['ready']
    assert not got['published_current']
    gates = {row['id']: row['ok'] for row in got['gates']}
    assert gates['compile']
    assert not gates['claims']
    assert scientific_dependency_digest(workspace) != workspace['manuscript']['scientific_dependency_digest']
    # Re-generating prose/TeX cannot resolve scientific contradictions.
    regenerated = _generate(workspace)
    _compiled(regenerated)
    assert not readiness(regenerated)['ready']
    assert not readiness(regenerated)['manuscript_audit']['passed']


def test_consistent_negative_results_still_allow_scoped_negative_manuscripts():
    workspace = _ready_workspace()
    workspace['runs'][0]['scientific_outcome'] = 'refuted'
    workspace['runs'][0]['tool_receipts'][0]['result_excerpt'] = json.dumps(
        observation_payload(workspace['protocol'],'evidence:1','refuted'))
    claim = workspace['claims'][0]
    claim.update(text='No improvement in the tested scope', outcome_scope='refuted')
    claim['evidence_digest'] = claim_evidence_digest(claim, workspace['runs'])
    workspace.update(normalize_program_fields(workspace))
    assert workspace['claims'][0]['validity'] == 'current'
    assert workspace['decision']['status'] == 'refute'
    workspace = _generate(workspace)
    _compiled(workspace)
    assert readiness(workspace)['ready']
    assert readiness(workspace)['published_current']


def test_conflicted_manuscript_snapshot_binds_uncited_counterevidence():
    workspace = _ready_workspace()
    opposing = copy.deepcopy(workspace['runs'][0])
    opposing.update(id='r2', scientific_outcome='refuted')
    workspace['runs'].append(opposing)
    workspace = _generate(workspace)
    reviewed = scientific_dependency_digest(workspace)
    assert reviewed == workspace['manuscript']['scientific_dependency_digest']
    for _ in range(3):
        workspace.update(normalize_program_fields(workspace))
        assert scientific_dependency_digest(workspace) == reviewed
    assert workspace['claims'][0]['evidence_refs'] == ['r1']
    workspace['runs'][1]['tool_receipts'][0]['result_digest'] = 'changed counterevidence'
    assert scientific_dependency_digest(workspace) != reviewed
    assert not readiness(workspace)['published_current']

"""Material objections survive settlement and cannot accompany acceptance."""
import json

import pytest

from tests.evolution.test_evolution_runs import OWNER, finish, review, runtime as runtime

pytestmark = pytest.mark.unit


def prepare(runtime, *, requirements=None):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Deliver source-consistent research.',
        'max_attempts': 1, **({'acceptance_requirements': requirements} if requirements else {})}, user_id=OWNER)
    finish(runtime, tasks[0], 'There were zero declarations.')
    return run, tasks[-1]


def objection(classification='contradicted', **extra):
    return {'criterion_id': 'evidence', 'classification': classification,
            'claim': 'There were zero declarations.',
            'evidence_ref': 'Frozen audit reports four declarations before final delivery failed.', **extra}


@pytest.mark.parametrize('classification,outcome', [('contradicted', 'fail'), ('unverified', 'unknown')])
def test_material_objection_refuses_positive_verdict_and_strategy(runtime, classification, outcome):
    from lib.evolution import get_episode
    runs, _, _ = runtime
    run, task = prepare(runtime, requirements=['Counts must agree with the source.'])
    value = json.loads(review())
    value['requirement_results'] = [{'requirement_id': 'requirement-1', 'outcome': 'pass', 'evidence_ref': 'Claimed accurate.'}]
    value['evidence_audit']['objections'] = [objection(classification, requirement_id='requirement-1')]
    final = finish(runtime, task, json.dumps(value))
    assert final['status'] == 'needs_attention'
    assert final['review']['evidence_audit'] == value['evidence_audit']
    assert final['review']['requirement_results'][0]['outcome'] == outcome
    rows = {row['criterion_id']: row for row in final['review']['criterion_results']}
    assert rows['evidence']['outcome'] == rows['objective']['outcome'] == outcome
    assert get_episode(run['episode_id'], user_id=OWNER)['status'] != 'success'
    assert final['learning']['steps']['strategies']['result']['templates'] == 0
    assert runs.advance_run(run['run_id'], user_id=OWNER, terminal_task=task) == final


def test_incomplete_audit_cannot_pass_even_with_no_recorded_objections(runtime):
    _, task = prepare(runtime)
    value = json.loads(review()); value['evidence_audit']['complete'] = False
    final = finish(runtime, task, json.dumps(value))
    assert final['status'] == 'needs_attention'
    assert final['review']['criterion_results'][0]['outcome'] == 'unknown'


def test_missing_required_audit_refuses_without_replaying_executor(runtime):
    _, task = prepare(runtime)
    value = json.loads(review()); value.pop('evidence_audit')
    final = finish(runtime, task, json.dumps(value))
    assert final['status'] == 'failed'
    assert final['review'] is None
    assert len(runtime[2]) == 2


def test_foreign_requirement_refuses_review(runtime):
    _, task = prepare(runtime)
    value = json.loads(review()); value['evidence_audit']['objections'] = [objection(requirement_id='requirement-7')]
    final = finish(runtime, task, json.dumps(value))
    assert final['status'] == 'failed' and final['review'] is None


def test_empty_complete_audit_allows_only_the_existing_acceptance(runtime):
    _, task = prepare(runtime)
    final = finish(runtime, task, review())
    assert final['status'] == 'completed'
    assert 'hypothesis_assessment' not in final['review']


def test_legacy_run_keeps_its_frozen_contract(runtime):
    runs, _, _ = runtime
    run, task = prepare(runtime)
    saved, version = runs._read(run['run_id'], OWNER)
    saved.pop('evidence_review_version'); runs._save(saved, version, OWNER)
    value = json.loads(review()); value.pop('evidence_audit')
    assert finish(runtime, task, json.dumps(value))['status'] == 'completed'


def test_audit_policy_is_frozen_in_terminal_checkpoint_review_input(runtime):
    from lib.evolution import stage_outcomes
    runs, _, tasks = runtime
    run, task = prepare(runtime)
    metadata = stage_outcomes.read_metadata(run['run_id'], tasks[0]['id'], user_id=OWNER)
    assert 'Return evidence_audit' in metadata['review_prompt']
    from lib.evolution.hypothesis_review import review_schema
    assert 'evidence_audit' in review_schema(runs.get_run(run['run_id'], user_id=OWNER))['required']


def test_unknown_does_not_weaken_a_failed_check():
    from lib.evolution.review_evidence import enforce
    value = json.loads(review('fail'))
    value['evidence_audit']['objections'] = [objection('unverified')]
    enforce({'evidence_review_version': 1}, value)
    assert all(row['outcome'] == 'fail' for row in value['criterion_results'])


def test_long_objection_evidence_is_losslessly_archived_without_rejudging(runtime):
    from lib.evolution import stage_outcomes
    run, task = prepare(runtime)
    value = json.loads(review())
    value['evidence_audit']['objections'] = [objection(evidence_ref='Complete unresolved source evidence. 中文😀' * 30)]
    raw = json.dumps(value, ensure_ascii=False)
    final = finish(runtime, task, raw)
    assert final['status'] == 'needs_attention' and len(runtime[2]) == 2
    assert final['review']['evidence_audit']['objections'][0]['classification'] == 'contradicted'
    assert final['review']['outcome'] == 'failure'
    marker = final['receipts'][-1]['review_format_normalization']
    assert marker['field_paths'] == ['/evidence_audit/objections/0/evidence_ref']
    assert stage_outcomes.page(run['run_id'], task['id'], user_id=OWNER)['content'] == raw


def test_objection_overflow_refuses_instead_of_silently_discarding(runtime):
    _, task = prepare(runtime)
    value = json.loads(review()); value['evidence_audit']['objections'] = [objection() for _ in range(9)]
    final = finish(runtime, task, json.dumps(value))
    assert final['status'] == 'failed' and final['review'] is None and len(runtime[2]) == 2


def test_terminal_receiver_never_waits_for_optional_external_refetch(runtime, monkeypatch):
    from lib.evolution import maintenance
    monkeypatch.setattr(maintenance, 'reindex_maintenance',
                        lambda **kw: pytest.fail('optional external fetch blocked terminal delivery'))
    _, task = prepare(runtime)
    final = finish(runtime, task, review())
    assert final['status'] == 'completed'
    assert final['learning']['complete'] is False
    assert final['learning']['steps']['refetch']['result']['deferred'] is True

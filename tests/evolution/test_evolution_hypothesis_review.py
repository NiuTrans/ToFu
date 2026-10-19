"""Exact hypotheses survive independent review without collapsing value axes."""
from copy import deepcopy
import json

import pytest

from tests.evolution.test_evolution_runs import OWNER, runtime as runtime, finish, review
from tests.evolution.test_evolution_exploration_tools import candidate
from lib.evolution.hypothesis_review import AXES, digest

pytestmark = pytest.mark.unit


def assessment(hypothesis, **outcomes):
    return {'hypothesis_sha256': digest(hypothesis), **{axis: {
        'outcome': outcomes.get(axis, 'unknown'), 'evidence_ref': f'Inspected {axis} evidence; unresolved claims remain unknown.'} for axis in AXES}}


def judgment(hypothesis, **outcomes):
    value = json.loads(review());value['hypothesis_assessment'] = assessment(hypothesis, **outcomes)
    return json.dumps(value)


def test_review_uses_frozen_hypothesis_even_when_submission_omits_original_falsifier(runtime):
    runs, _, tasks = runtime
    source = candidate()['hypothesis'];original = deepcopy(source)
    run = runs.submit_run({'objective': 'Test a future application scenario.'}, user_id=OWNER,
                          idempotency_key='bound-hypothesis', exploration_hypothesis=source)
    source['falsifier'] = 'Changed easier falsifier'
    reviewing = finish(runtime, tasks[0], 'Prototype works; original falsifier deliberately omitted.')
    prompt = tasks[1]['messages'][0]['content']
    assert original['falsifier'] in prompt and 'Changed easier falsifier' not in prompt
    assert digest(original) in prompt and 'prototype success' in prompt
    assert reviewing['exploration_hypothesis'] == original
    current = finish(runtime, tasks[1], judgment(original, feasibility='pass'))
    assert current['status'] == 'completed'
    checked = current['review']['hypothesis_assessment']
    assert checked['feasibility']['outcome'] == 'pass'
    assert all(checked[axis]['outcome'] == 'unknown' for axis in ('problem', 'novelty', 'net_value'))
    assert current['exploration_hypothesis'] == original


def test_owned_terminal_capsule_freezes_the_exact_hypothesis_in_review_input(runtime, monkeypatch):
    from lib.tasks_pkg.manager import _persist, _terminal
    from lib.agent_core.execution_session import ExecutionPhase
    from tests.evolution.test_evolution_stage_outcomes import terminal
    runs, _, tasks = runtime
    hypothesis = candidate()['hypothesis']
    run = runs.submit_run({'objective': 'Keep the original novel mechanism across process loss.'},
                         user_id=OWNER, exploration_hypothesis=hypothesis)
    task = terminal(tasks[0])
    task['_executionSession'].settle(ExecutionPhase.COMPLETED, cause='controlled')
    with monkeypatch.context() as patch:
        patch.setattr(_terminal, 'notify_terminal_listeners', lambda _: (_ for _ in ()).throw(RuntimeError('lost callback')))
        assert _persist.persist_task_result(task, _defer_heavy_release=True) is False
    monkeypatch.setattr(runs, 'default_task_loader', lambda *a, **kw: None)
    resumed = runs.advance_run(run['run_id'], user_id=OWNER)
    assert resumed['status'] == 'reviewing' and len(tasks) == 2
    assert digest(hypothesis) in tasks[1]['messages'][0]['content']
    assert hypothesis['mechanism'] in tasks[1]['messages'][0]['content']


@pytest.mark.parametrize('fault', ['absent', 'foreign', 'missing_axis', 'empty_evidence', 'extra_axis'])
def test_review_cannot_substitute_or_omit_hypothesis_assessment(runtime, fault):
    runs, _, tasks = runtime
    hypothesis = candidate()['hypothesis']
    run = runs.submit_run({'objective': 'Assess the exact claim.'}, user_id=OWNER, exploration_hypothesis=hypothesis)
    finish(runtime, tasks[0], 'Observed prototype.')
    value = json.loads(judgment(hypothesis, feasibility='pass'))
    a = value['hypothesis_assessment']
    if fault == 'absent': del value['hypothesis_assessment']
    elif fault == 'foreign': a['hypothesis_sha256'] = '0' * 64
    elif fault == 'missing_axis': del a['problem']
    elif fault == 'empty_evidence': a['novelty']['evidence_ref'] = '   '
    elif fault == 'extra_axis': a['confidence'] = 1
    failed = finish(runtime, tasks[1], json.dumps(value))
    assert failed['status'] != 'completed' and failed['review'] is None
    assert len(tasks) == 2


def test_unbound_assessment_cannot_create_value_claim_for_legacy_run(runtime):
    runs, _, tasks = runtime
    run = runs.submit_run({'objective': 'Legacy objective.'}, user_id=OWNER)
    finish(runtime, tasks[0], 'Delivery.')
    failed = finish(runtime, tasks[1], judgment(candidate()['hypothesis'], net_value='pass'))
    assert failed['status'] != 'completed' and failed['review'] is None


def test_idempotency_refuses_changed_hypothesis_before_new_dispatch(runtime):
    runs, _, tasks = runtime
    original = candidate()['hypothesis']
    payload = {'objective': 'Same objective cannot hide a changed falsifier.'}
    first = runs.submit_run(payload, user_id=OWNER, idempotency_key='claim-id', exploration_hypothesis=original)
    assert runs.submit_run(payload, user_id=OWNER, idempotency_key='claim-id', exploration_hypothesis=deepcopy(original)) == first
    changed = deepcopy(original);changed['falsifier'] = 'Lowered acceptance threshold.'
    with pytest.raises(runs.EvolutionStateError, match='Idempotency'):
        runs.submit_run(payload, user_id=OWNER, idempotency_key='claim-id', exploration_hypothesis=changed)
    assert len(tasks) == 1


def test_missing_full_original_cannot_leave_positive_axes_as_verified():
    from lib.evolution import runs
    h = candidate()['hypothesis']
    task = {'content': judgment(h, **dict.fromkeys(AXES, 'pass')), '_userId': OWNER}
    run = {'exploration_hypothesis': h, 'result_source': {
        'task_id': 'original', 'source_sha256': '1' * 64, 'total_chars': 50000}}
    parsed = runs._parse_review(task, run=run)
    assert all(parsed['hypothesis_assessment'][axis]['outcome'] == 'unknown' for axis in AXES)
    assert parsed['outcome'] == 'failure'


def test_claims_are_not_admitted_through_public_run_request(runtime):
    runs, _, tasks = runtime
    with pytest.raises(runs.EvolutionContractError):
        runs.submit_run({'objective': 'Unauthorized run claim.',
                         'exploration_hypothesis': candidate()['hypothesis']}, user_id=OWNER)
    assert not tasks


def test_long_axis_evidence_uses_lossless_original_pointer_without_changing_judgment(runtime):
    runs, _, tasks = runtime
    h = candidate()['hypothesis']
    runs.submit_run({'objective': 'Retain the complete independent rationale.'}, user_id=OWNER,
                    exploration_hypothesis=h)
    finish(runtime, tasks[0], 'Controlled prototype output.')
    raw = json.loads(judgment(h, feasibility='pass'))
    raw['hypothesis_assessment']['problem']['evidence_ref'] = '原始需求证据不足 🧪' * 70
    current = finish(runtime, tasks[1], json.dumps(raw, ensure_ascii=False))
    assert current['status'] == 'completed'
    checked = current['review']['hypothesis_assessment']
    assert checked['hypothesis_sha256'] == digest(h) and checked['problem']['outcome'] == 'unknown'
    assert 'JSON pointer /hypothesis_assessment/problem/evidence_ref' in checked['problem']['evidence_ref']
    marker = current['receipts'][-1]['review_format_normalization']
    assert marker['field_paths'] == ['/hypothesis_assessment/problem/evidence_ref']


def test_next_experiment_context_keeps_unresolved_value_axes_without_copying_full_review(runtime):
    from lib.evolution import programs
    runs, _, tasks = runtime
    proposed = candidate();h = proposed['hypothesis']
    final = {'kind': 'final', 'title': 'Audit original objective',
             'instruction': 'Verify original acceptance.', 'checks': ['Original acceptance has evidence.']}
    p = programs.create_program({'objective': 'Find a valuable future scenario.', 'steps': [proposed, final]},
        user_id=OWNER, idempotency_key='value-memory')
    programs.command_program(p['program_id'], {'action': 'start'}, user_id=OWNER)
    programs.advance_program(p['program_id'], user_id=OWNER)
    p = programs.advance_program(p['program_id'], user_id=OWNER)
    reviewed = finish(runtime, tasks[0], 'Observed controlled prototype.')
    value = json.loads(judgment(h, feasibility='pass'))
    value['requirement_results'] = [{'requirement_id': key, 'outcome': 'pass',
        'evidence_ref': 'Controlled experiment keeps consumer value unresolved.'} for key in runs._requirements(reviewed)]
    finish(runtime, tasks[1], json.dumps(value))
    programs.advance_program(p['program_id'], user_id=OWNER)
    pending = programs.advance_program(p['program_id'], user_id=OWNER)
    seed = json.loads(pending['current']['context_seed'])
    memory = seed['recent_experiments'][0]
    assert memory['outcome'] == 'completed'
    assert memory['hypothesis_assessment'] == {'hypothesis_sha256': digest(h),
        'problem': 'unknown', 'feasibility': 'pass', 'novelty': 'unknown', 'net_value': 'unknown'}
    assert 'Inspected problem evidence' not in pending['current']['context_seed']
    assert len(pending['current']['context_seed']) <= 24000 and len(tasks) == 2


def test_provider_visible_hypothesis_output_schema_is_complete_without_external_references():
    import jsonschema
    from lib.evolution.hypothesis_review import review_schema
    h = candidate()['hypothesis']
    schema = review_schema({'exploration_hypothesis': h})
    valid = json.loads(judgment(h))
    jsonschema.validate(valid, schema)
    absent = deepcopy(valid);del absent['hypothesis_assessment']
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(absent, schema)
    incomplete = deepcopy(valid);del incomplete['hypothesis_assessment']['net_value']
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(incomplete, schema)
    jsonschema.validate(json.loads(review()), review_schema({}))


from tests.evolution.test_evolution_background_execution import command as _background_command_fixture
background_command = _background_command_fixture


@pytest.mark.parametrize('coverage',['none','partial','complete'])
def test_positive_axes_require_complete_model_observed_background_original(runtime,background_command,coverage):
    from tests.evolution.test_evolution_background_execution import attach_background_launch
    from tests.evolution.test_evolution_source_tools import delivered
    from lib.tasks_pkg import background_commands
    from lib.project_mod.run_command import _format_run_output
    runs,_,tasks=runtime;hypothesis=candidate()['hypothesis']
    run=runs.submit_run({'objective':'Assess the mechanism from the late measured original','require_command_execution':True,'max_attempts':1},
        user_id=OWNER,exploration_hypothesis=hypothesis)
    executor=tasks[0];attach_background_launch(background_command,executor)
    assert finish(runtime,executor,'Awaiting original measured evidence.')['background_wait']
    background_commands.finish_handed_off_command(command_id=background_command[1],result=_format_run_output(
        'experiment','前段观测 🧪\n'*1000+' BACKGROUND_FALSIFIER: network saturated','',0),error=None)
    reviewing=runs.advance_run(run['run_id'],user_id=OWNER)
    assert reviewing['status']=='reviewing'
    reference=reviewing['background_evidence_source'];reviewer=tasks[-1]
    if coverage!='none':
        offset=0
        while True:
            page=delivered(reviewer,{'action':'read','run_id':run['run_id'],'task_id':reference['task_id'],
                'source_sha256':reference['source_sha256'],'source_kind':'background_evidence','offset':offset})
            if coverage=='partial':
                assert page['next_offset'] is not None
                break
            if page['next_offset'] is None:break
            offset=page['next_offset']
    value=json.loads(judgment(hypothesis,problem='fail',feasibility='pass',novelty='pass',net_value='pass'))
    settled=finish(runtime,reviewer,json.dumps(value))
    assessed=settled['review']['hypothesis_assessment']
    assert assessed['problem']['outcome']=='fail'
    for axis in ('feasibility','novelty','net_value'):
        assert assessed[axis]['outcome']==('pass' if coverage=='complete' else 'unknown')
    assert assessed['hypothesis_sha256']==digest(hypothesis)

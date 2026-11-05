"""Shared capability selection survives programme stages and independent review."""
from copy import deepcopy
import pytest

from tests.skills.test_skill_activation import skill_workspace as _isolated, _package
from tests.evolution.test_evolution_runs import runtime as _runtime, OWNER, finish, review
from tests.evolution.test_evolution_programs import runtime as _program_runtime, OWNER as PROGRAM_OWNER, launch

isolated = _isolated
runtime = _runtime
program_runtime = _program_runtime

pytestmark = pytest.mark.unit


def selection(isolated):
    project = isolated / 'project'
    path = _package(project, '.tofu/skills', 'novelty', 'Require an observed novelty kill-test.')
    return {'objective': 'Investigate with falsifiable evidence.', 'project_path': str(project),
            'skill_selection': ['novelty']}, path


def test_run_reuses_common_composer_and_independent_requirement_review(runtime, isolated):
    runs, _, tasks = runtime
    payload, _ = selection(isolated)
    run = runs.submit_run(payload, user_id=OWNER, idempotency_key='required-guide')
    assert run['skill_bindings'][0]['skill_id'] == 'novelty'
    assert tasks[0]['config']['skillSelection'] == run['skill_bindings']
    assert 'novelty' in tasks[0]['messages'][0]['content']
    finish(runtime, tasks[0], 'Claimed completion without the check.')
    cfg = tasks[1]['config']
    assert cfg['skillSelection'] == run['skill_bindings']
    assert cfg['skillSelectionMode'] == 'review'
    assert not cfg.get('projectEnabled') and not cfg.get('codeExecEnabled')
    assert cfg['plugins'] == [] and cfg['toolSpecAllowlist'] == ['skills']
    # Aggregate success cannot omit the selected workflow requirement.
    result = finish(runtime, tasks[1], review())
    assert result['status'] != 'completed'


def test_unavailable_selection_admits_no_task(runtime):
    runs, _, tasks = runtime
    with pytest.raises(runs.EvolutionContractError, match='missing'):
        runs.submit_run({'objective': 'Check mechanisms', 'skill_selection': ['missing']},
                        user_id=OWNER, idempotency_key='missing-guide')
    assert tasks == []


def test_program_pins_guide_and_passes_it_to_every_experiment(program_runtime, isolated):
    programs, _, launched = program_runtime
    payload, package = selection(isolated)
    programme = programs.create_program(payload, user_id=PROGRAM_OWNER, idempotency_key='bound-plan')
    assert programme['execution_options']['skill_bindings'][0]['skill_id'] == 'novelty'
    assert launched == []
    launch(program_runtime, programme)
    assert launched[0]['payload']['skill_bindings'] == programme['execution_options']['skill_bindings']
    (package / 'SKILL.md').write_text('---\nname: novelty\n---\nDifferent checks')
    # Retrieving an accepted request is idempotent; a new stage cannot rebind.
    retained = programs.create_program(payload, user_id=PROGRAM_OWNER, idempotency_key='bound-plan')
    assert retained['execution_options'] == programme['execution_options']
    from lib.evolution.execution_capabilities import bind_selection
    with pytest.raises(programs.EvolutionContractError, match='changed'):
        bind_selection(retained['execution_options'], user_id=PROGRAM_OWNER)


def test_research_context_reuses_workspace_contract_and_reaches_review(runtime):
    from lib.research.workspace import empty_workspace
    runs, _, tasks = runtime
    workspace = empty_workspace('Synthetic methods')
    context = {key: deepcopy(workspace[key]) for key in ('hypothesis', 'protocol', 'capability_bindings')}
    context['hypothesis'] = 'Mechanism must beat the matched baseline.'
    context['protocol']['falsifier'] = 'No lift under the held-out matched budget.'
    run = runs.submit_run({'objective': 'Measure the mechanism.', 'research_context': context},
                          user_id=OWNER, idempotency_key='protocol')
    assert run['research_context'] == context
    assert context['protocol']['falsifier'] in tasks[0]['messages'][0]['content']
    finish(runtime, tasks[0], 'The experiment is inconclusive.')
    assert context['protocol']['falsifier'] in tasks[1]['messages'][0]['content']
    assert len(runs._requirements(run)) == 1
    assert not run.get('scientific_protocol')  # Input does not invent numerical proof.


def test_missing_bound_tool_refuses_before_task_dispatch(runtime, monkeypatch):
    from lib.research.workspace import empty_workspace
    from lib.research import capabilities
    runs, _, tasks = runtime
    workspace = empty_workspace('Synthetic methods')
    context = {key: workspace[key] for key in ('hypothesis', 'protocol', 'capability_bindings')}
    context['capability_bindings'] = [{'capability': 'experiment.execute', 'provider': 'synthetic',
        'tool': 'mcp__synthetic__run', 'schema_hash': 'original', 'enabled': True,
        'argument_defaults': {}, 'notes': ''}]
    monkeypatch.setattr(capabilities, 'build_capability_catalog', lambda **kw: {'tools': []})
    with pytest.raises(runs.EvolutionContractError, match='tool_unavailable'):
        runs.submit_run({'objective': 'Measure the mechanism.', 'research_context': context},
                        user_id=OWNER, idempotency_key='unavailable-tool')
    assert tasks == []

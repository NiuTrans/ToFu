"""Flow-template crystallization: ≥3 wins crystallize the choreography itself."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

USER = 77


@pytest.fixture
def store(monkeypatch):
    from tests.support.evolution_store import install_store
    return install_store(monkeypatch)


def _success_episode(episode_id, task_id):
    from lib.evolution import records
    records.put_record(records.NAMESPACE_EPISODES, f'{USER}:{episode_id}', {
        'schema_version': 1, 'episode_id': episode_id, 'goal_id': 'g',
        'plan_id': 'p', 'selected_path_id': 'a', 'exploratory': False,
        'task_id': task_id, 'status': 'success',
        'created_at': 1, 'updated_at': 2, 'settled_at': 2,
        'actuals': {'tokens': 1, 'wall_clock_seconds': 1, 'tool_calls': 1},
        'verdict': {'outcome': 'success', 'assessed_by': 'checker',
                    'criterion_results': []},
        'learning': {'resource_weight_deltas': [],
                     'cost_actual_vs_estimate': None,
                     'strategy_outcome': None, 'capability_gap': None},
    }, kind='episode')


def _tasks(*sequences):
    return {f'task-{i}': {'toolRounds': [{'tool_calls': [
        {'name': name} for name in seq]}]} for i, seq in enumerate(sequences)}


def _loader(tasks):
    return lambda task_id, **_: tasks.get(task_id)


def test_synthesized_definition_passes_the_orchestration_contract():
    from lib.evolution.flow_templates import build_flow_definition
    from lib.orchestration._validate import validate_definition
    definition = build_flow_definition(('search', 'fetch', 'summarize'),
                                       title='search > fetch > summarize')
    verdict = validate_definition(definition)
    assert verdict.get('errors') == []
    roles = [n.get('role') for n in definition['nodes']
             if n.get('type') == 'role']
    assert roles == ['worker', 'critic']


def test_three_wins_propose_a_flow_template_once(store):
    from lib import evolution
    for i in range(3):
        _success_episode(f'e{i}', f'task-{i}')
    tasks = _tasks(('search', 'fetch', 'summarize'),
                   ('search', 'fetch', 'summarize'),
                   ('search', 'fetch', 'summarize'))
    from lib.evolution import crystallize
    proposals = crystallize.propose_flow_template_crystallizations(
        user_id=USER, loader=_loader(tasks))
    assert len(proposals) == 1
    mutation = proposals[0]
    assert mutation['kind'] == 'flow_template'
    assert mutation['payload']['sequence'] == ['search', 'fetch', 'summarize']
    assert len(mutation['evidence_episode_ids']) == 3
    # Two wins is macro territory only: no flow proposal below the bar.
    store2_ids = [m['mutation_id'] for m in evolution.list_mutations(
        user_id=USER, kind='flow_template')]
    assert len(store2_ids) == 1
    again = crystallize.propose_flow_template_crystallizations(
        user_id=USER, loader=_loader(tasks))
    assert again == []


def test_gate_sandbox_rejects_an_invalid_carried_definition(store):
    from lib.evolution import flow_templates
    good = flow_templates.build_flow_definition(('a', 'b'), title='a > b')
    flow_templates.validate_flow_payload({'definition': good})
    with pytest.raises(Exception, match='invalid'):
        flow_templates.validate_flow_payload(
            {'definition': {'schema': 'tofu.orchestration/v1', 'name': '',
                            'nodes': [], 'edges': []}})
    with pytest.raises(Exception, match='must be an object'):
        flow_templates.validate_flow_payload({'definition': 'nope'})


def test_activation_materializes_the_definition_and_failure_stays_confirmed(
        store):
    from lib import evolution
    from lib.evolution import flow_templates
    _success_episode('e1', 'task-0')
    mutation = evolution.propose_mutation(
        'flow_template',
        {'sequence': ['a', 'b'], 'title': 'a > b',
         'definition': flow_templates.build_flow_definition(('a', 'b'),
                                                            title='a > b')},
        ['e1'], user_id=USER)
    # Structural sandbox blocks confirmation on a tampered definition.
    tampered = evolution.propose_mutation(
        'flow_template',
        {'sequence': ['c', 'd'], 'title': 'c > d', 'definition': {'name': 'x'}},
        ['e1'], user_id=USER)
    with pytest.raises(evolution.EvolutionStateError):
        evolution.confirm_mutation(tampered['mutation_id'], user_id=USER)

    created = []
    class _Result:
        def __init__(self, entry):
            self.entry = entry
    monkey_creator = lambda definition, uid: created.append(
        (definition, uid)) or _Result({'id': 'orch-1'})
    evolution.confirm_mutation(mutation['mutation_id'], user_id=USER)
    evolution.run_regression(mutation, user_id=USER)
    import lib.evolution.gate as gate_module
    gate_module_materialize = flow_templates.materialize_flow_template
    try:
        flow_templates.materialize_flow_template = (
            lambda record, *, user_id, definition_creator=None:
            gate_module_materialize(
                record, user_id=user_id,
                definition_creator=monkey_creator))
        evolution.activate_mutation(mutation['mutation_id'], user_id=USER,
                                    allow_unmeasurable=True)
    finally:
        flow_templates.materialize_flow_template = gate_module_materialize
    assert created and created[0][1] == USER
    assert created[0][0]['name'].startswith('结晶流程')

    # A refused create keeps the mutation confirmed and retryable.
    second = evolution.propose_mutation(
        'flow_template',
        {'sequence': ['e', 'f'], 'title': 'e > f',
         'definition': flow_templates.build_flow_definition(('e', 'f'),
                                                            title='e > f')},
        ['e1'], user_id=USER)
    evolution.confirm_mutation(second['mutation_id'], user_id=USER)
    evolution.run_regression(second, user_id=USER)
    try:
        flow_templates.materialize_flow_template = (
            lambda record, *, user_id, definition_creator=None:
            gate_module_materialize(
                record, user_id=user_id,
                definition_creator=lambda d, u: _Result(None)))
        with pytest.raises(evolution.EvolutionStateError,
                           match='not accepted'):
            evolution.activate_mutation(second['mutation_id'], user_id=USER,
                                        allow_unmeasurable=True)
    finally:
        flow_templates.materialize_flow_template = gate_module_materialize
    reloaded = [m for m in evolution.list_mutations(user_id=USER)
                if m['mutation_id'] == second['mutation_id']][0]
    assert reloaded['status'] == 'confirmed'

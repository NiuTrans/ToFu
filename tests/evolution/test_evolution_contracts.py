"""Guard tests for the self-evolution P0 contracts.

Design authority: docs/EVOLUTION_DESIGN.md. The invariants pinned here are the
ones later phases rely on: verifiable criteria or explicit exploratory mode,
cost priors with machine-checkable provenance (model prose is not
representable), and episode learning deltas bounded to their stores.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.unit

CONTRACTS = Path(__file__).resolve().parents[2] / 'contracts'


def _schema(name):
    return json.loads((CONTRACTS / name).read_text(encoding='utf-8'))


def _valid_goal():
    return {
        'schema_version': 1,
        'goal_id': 'goal-1',
        'statement': 'Reproduce the published result within 10% error.',
        'domain': 'research',
        'lang': 'en',
        'exploratory': False,
        'criteria': [
            {'criterion_id': 'c1', 'kind': 'test_command',
             'params': {'command': 'pytest -q tests/test_metric.py'}, 'weight': 1.0},
        ],
        'budget_envelope': {'max_tokens': 200000, 'max_wall_clock_seconds': 7200,
                            'max_tool_calls': 200},
        'constraints': ['no network outside allowlist'],
        'status': 'admitted',
        'created_at': 1700000000,
    }


def _valid_plan():
    return {
        'schema_version': 1,
        'plan_id': 'plan-1',
        'goal_id': 'goal-1',
        'plan_revision': 1,
        'candidates': [
            {
                'path_id': 'path-a',
                'title': 'Adapt the reference implementation',
                'stages': [{'kind': 'execute', 'detail': 'port then evaluate'}],
                'resource_refs': [
                    {'ref_id': 'gh:owner/repo', 'indexer': 'project',
                     'locator': 'https://github.com/owner/repo', 'relevance': 0.9,
                     'provenance': 'github search'},
                ],
                'cost_estimate': {
                    'expected_tokens': 50000, 'expected_wall_clock_seconds': 1800,
                    'expected_tool_calls': 60, 'failure_rate_prior': 0.3,
                    'expected_information_gain': 0.7, 'expected_utility': 0.6,
                    'priors_source': 'measured_history',
                },
                'utility_over_cost': 1.4,
            },
        ],
        'selected_path_id': 'path-a',
        'selection_rationale': 'highest utility over cost under envelope',
        'requires_confirmation': True,
        'status': 'proposed',
        'created_at': 1700000001,
    }


def _valid_episode():
    return {
        'schema_version': 1,
        'episode_id': 'ep-1',
        'goal_id': 'goal-1',
        'plan_id': 'plan-1',
        'selected_path_id': 'path-a',
        'exploratory': False,
        'task_id': 'task-1',
        'status': 'success',
        'created_at': 1700000002,
        'updated_at': 1700001002,
        'settled_at': 1700001002,
        'actuals': {'tokens': 47000, 'wall_clock_seconds': 1650, 'tool_calls': 55},
        'stage_receipts': [{'stage': 'execute', 'status': 'done'}],
        'verdict': {
            'outcome': 'success',
            'assessed_by': 'checker',
            'criterion_results': [
                {'criterion_id': 'c1', 'outcome': 'pass',
                 'evidence_ref': 'task-1:pytest'},
            ],
            'evidence_refs': ['task-1:pytest'],
        },
        'learning': {
            'resource_weight_deltas': [
                {'ref_id': 'gh:owner/repo', 'indexer': 'project', 'delta': 0.2,
                 'basis': 'cited_in_success'},
            ],
            'cost_actual_vs_estimate': {'tokens_ratio': 0.94, 'wall_clock_ratio': 0.92},
            'strategy_outcome': {'distilled_template_id': None, 'anti_pattern': None},
            'capability_gap': None,
        },
    }


def test_valid_goal_plan_episode_validate():
    # jsonschema.validate returns None on success and raises on drift; the
    # explicit assertions keep the "valid fixtures stay valid" contract
    # visible to structural audits instead of relying on no-throw alone.
    assert jsonschema.validate(_valid_goal(), _schema('evolution_goal_v1.schema.json')) is None
    assert jsonschema.validate(_valid_plan(), _schema('evolution_plan_v1.schema.json')) is None
    assert jsonschema.validate(_valid_episode(), _schema('evolution_episode_v1.schema.json')) is None


def test_exploratory_goal_must_not_carry_criteria():
    goal = _valid_goal()
    goal['exploratory'] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(goal, _schema('evolution_goal_v1.schema.json'))
    goal['criteria'] = []
    jsonschema.validate(goal, _schema('evolution_goal_v1.schema.json'))


def test_non_exploratory_goal_requires_at_least_one_criterion():
    goal = _valid_goal()
    goal['criteria'] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(goal, _schema('evolution_goal_v1.schema.json'))


def test_goal_rejects_unknown_criterion_kind_and_extra_fields():
    goal = _valid_goal()
    goal['criteria'][0]['kind'] = 'vibes'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(goal, _schema('evolution_goal_v1.schema.json'))
    goal = _valid_goal()
    goal['owner_nickname'] = 'me'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(goal, _schema('evolution_goal_v1.schema.json'))


def test_plan_cost_priors_must_carry_provenance():
    plan = _valid_plan()
    del plan['candidates'][0]['cost_estimate']['priors_source']
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, _schema('evolution_plan_v1.schema.json'))
    plan = _valid_plan()
    plan['candidates'][0]['cost_estimate']['priors_source'] = 'model_guess'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, _schema('evolution_plan_v1.schema.json'))


def test_plan_selection_is_never_autonomous():
    plan = _valid_plan()
    plan['requires_confirmation'] = False
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, _schema('evolution_plan_v1.schema.json'))
    plan = _valid_plan()
    plan['candidates'] = []
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(plan, _schema('evolution_plan_v1.schema.json'))


def test_episode_learning_deltas_stay_bounded():
    episode = _valid_episode()
    episode['learning']['resource_weight_deltas'][0]['delta'] = 1.5
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(episode, _schema('evolution_episode_v1.schema.json'))
    episode = _valid_episode()
    del episode['learning']['capability_gap']
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(episode, _schema('evolution_episode_v1.schema.json'))


def test_episode_actuals_come_from_receipts_not_negative_prose():
    episode = _valid_episode()
    episode['actuals']['tokens'] = -1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(episode, _schema('evolution_episode_v1.schema.json'))
    running = _valid_episode()
    running['status'] = 'running'
    running['verdict'] = None
    running['settled_at'] = None
    jsonschema.validate(running, _schema('evolution_episode_v1.schema.json'))


def test_episode_strategy_outcome_shape_is_explicit_even_when_null():
    episode = _valid_episode()
    episode['learning']['strategy_outcome'] = {'distilled_template_id': 'tmpl-9'}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(episode, _schema('evolution_episode_v1.schema.json'))
    episode = copy.deepcopy(_valid_episode())
    episode['learning']['strategy_outcome'] = {
        'distilled_template_id': 'tmpl-9', 'anti_pattern': 'never trust prose costs'}
    jsonschema.validate(episode, _schema('evolution_episode_v1.schema.json'))


def test_cost_priors_strategy_antipattern_mutation_gap_contracts_validate():
    jsonschema.validate({
        'schema_version': 1, 'domain': 'code', 'samples': 3,
        'tokens_median': 100, 'wall_clock_median': 10, 'tool_calls_median': 2,
        'failure_rate': 0.34, 'updated_at': 1},
        _schema('evolution_cost_priors_v1.schema.json'))
    jsonschema.validate({
        'schema_version': 1, 'template_id': 'tmpl-1', 'domain': 'code',
        'stages': [{'kind': 'execute', 'detail': 'd'}],
        'applicability_terms': ['reproduce', 'baseline'],
        'lineage': ['e1'], 'samples': 1, 'success_rate': 1.0,
        'created_at': 1, 'updated_at': 1},
        _schema('strategy_template_v1.schema.json'))
    with pytest.raises(jsonschema.ValidationError):
        # One episode is an incident, not a pattern: lineage minItems 2.
        jsonschema.validate({
            'schema_version': 1, 'pattern_id': 'anti-1', 'domain': 'code',
            'signature': 'failed:train', 'advice': 'avoid', 'lineage': ['e1'],
            'samples': 1, 'created_at': 1, 'updated_at': 1},
            _schema('evolution_anti_pattern_v1.schema.json'))
    jsonschema.validate({
        'schema_version': 1, 'pattern_id': 'anti-1', 'domain': 'code',
        'signature': 'failed:train', 'advice': 'avoid', 'lineage': ['e1', 'e2'],
        'samples': 2, 'created_at': 1, 'updated_at': 1},
        _schema('evolution_anti_pattern_v1.schema.json'))
    mutation = {
        'schema_version': 1, 'mutation_id': 'mut-1', 'kind': 'routing_change',
        'payload': {'model': 'a'}, 'evidence_episode_ids': ['e1'],
        'status': 'proposed', 'version': 1, 'parent_version': 0,
        'requires_confirmation': True, 'created_at': 1, 'updated_at': 1}
    jsonschema.validate(mutation, _schema('evolution_mutation_v1.schema.json'))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**mutation, 'evidence_episode_ids': []},
                            _schema('evolution_mutation_v1.schema.json'))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**mutation, 'requires_confirmation': False},
                            _schema('evolution_mutation_v1.schema.json'))
    jsonschema.validate({
        'schema_version': 1, 'gap_id': 'gap-1', 'summary': 'no ocr',
        'proposed_action': 'add tool', 'status': 'proposed',
        'lineage': ['e1'], 'samples': 1, 'created_at': 1, 'updated_at': 1},
        _schema('evolution_capability_gap_v1.schema.json'))
    jsonschema.validate({
        'schema_version': 1, 'item_id': 'ho-1', 'domain': 'code',
        'statement': 's',
        'criteria': [{'criterion_id': 'c', 'kind': 'test_command',
                      'params': {'command': 'true'}}],
        'baseline_weight': 1.0, 'lineage': ['e1'], 'created_at': 1},
        _schema('evolution_holdout_v1.schema.json'))
    receipt = {
        'schema_version': 1, 'receipt_id': 'reg-1', 'mutation_id': 'm1',
        'verdict': 'pass', 'score': 1.0, 'baseline': 0.5,
        'item_results': [{'item_id': 'ho-1', 'outcome': 'pass'}],
        'created_at': 1}
    jsonschema.validate(receipt, _schema('evolution_regression_v1.schema.json'))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**receipt, 'verdict': 'maybe'},
                            _schema('evolution_regression_v1.schema.json'))


def test_exploratory_research_may_record_only_unverified_falsification_intent():
    schema = _schema('evolution_goal_v1.schema.json')
    goal = _valid_goal()
    goal.update(domain='research', exploratory=True, criteria=[{
        'criterion_id': 'falsification', 'kind': 'falsification_protocol',
        'params': {'direction': 'an untested future mechanism'}}])
    jsonschema.validate(goal, schema)
    goal['criteria'][0]['kind'] = 'judge_panel'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(goal, schema)
    goal['criteria'][0]['kind'] = 'falsification_protocol'
    goal['domain'] = 'general'
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(goal, schema)

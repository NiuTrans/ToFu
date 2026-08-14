"""Control-node FieldSpec schema and validator contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.orchestration import (
    CONTROL_KINDS,
    CONTROL_PARAM_SCHEMA,
    VALID_ARTIFACT_FORMATS,
    VALID_HUMAN_MODES,
    VALID_PARAM_KINDS,
    control_param_schema,
    resolve_control_param,
    validate_definition,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def _definition(kind: str, params: dict) -> dict:
    return {
        'schema': 'tofu.orchestration/v1',
        'name': f'{kind} flow',
        'nodes': [
            {'id': 'start', 'type': 'control', 'kind': 'start'},
            {'id': 'node', 'type': 'control', 'kind': kind, 'params': params},
            {'id': 'stop', 'type': 'control', 'kind': 'stop'},
        ],
        'edges': [
            {'from': 'start', 'to': 'node'},
            {'from': 'node', 'to': 'stop'},
        ],
    }


def test_every_control_has_one_well_formed_schema():
    assert set(CONTROL_PARAM_SCHEMA) == set(CONTROL_KINDS)
    for kind, fields in CONTROL_PARAM_SCHEMA.items():
        assert control_param_schema(kind) is fields
        keys = [field['key'] for field in fields]
        assert len(keys) == len(set(keys)), kind
        for field in fields:
            assert field['kind'] in VALID_PARAM_KINDS
            assert field['label'].startswith('orch.')
            if field['kind'] == 'select':
                choices = field.get('options') or []
                assert choices
                assert all(choice['value'] and choice['label'].startswith('orch.')
                           for choice in choices)


def test_topology_facts_are_not_shadow_control_params():
    assert control_param_schema('parallel') == []
    branch_keys = {field['key'] for field in control_param_schema('branch')}
    assert branch_keys == {'classifier'}
    assert 'branches' not in branch_keys


def test_control_values_resolve_through_one_detached_default_port():
    assert resolve_control_param(
        {'type': 'control', 'kind': 'human'}, 'mode') == 'approve'
    assert resolve_control_param(
        {'type': 'control', 'kind': 'artifact'}, 'format') == 'file'
    assert resolve_control_param({
        'type': 'control', 'kind': 'loop',
        'params': {'max_iterations': 0},
    }, 'max_iterations') == 0
    assert resolve_control_param({
        'type': 'control', 'kind': 'human', 'params': {'mode': 'notify'},
    }, 'mode') == 'notify'
    assert resolve_control_param(
        {'params': {}}, 'mode', kind='human') == 'approve'


@pytest.mark.parametrize(
    ('kind', 'params', 'fragment'),
    [
        ('loop', {'max_iterations': 'ten'}, "'max_iterations' must be an integer"),
        ('loop', {'max_iterations': 0}, "'max_iterations' must be >= 1"),
        ('human', {'mode': 'maybe'}, 'human mode must be one of'),
        ('human', {'prompt': ['not text']}, "'prompt' must be a string"),
        ('artifact', {'path': 42}, "'path' must be a string"),
    ],
)
def test_control_values_are_validated_from_field_specs(kind, params, fragment):
    verdict = validate_definition(_definition(kind, params))
    assert verdict['ok'] is False
    assert any(fragment in error for error in verdict['errors'])


def test_unknown_legacy_shadow_param_warns_instead_of_blocking():
    verdict = validate_definition(_definition('parallel', {
        'max_concurrent': 8,
        'per_item': True,
    }))
    assert verdict['ok'] is True
    text = '\n'.join(verdict['warnings'])
    assert "unknown param 'max_concurrent'" in text
    assert "unknown param 'per_item'" in text


def test_loop_runtime_ceiling_is_visible_without_rejecting_portable_graphs():
    spec = next(field for field in control_param_schema('loop')
                if field['key'] == 'max_iterations')
    verdict = validate_definition(_definition('loop', {
        'max_iterations': spec['runtimeMax'] + 1,
    }))

    assert spec['runtimeMax'] == 12
    assert verdict['ok'] is True
    assert any(issue['code'] == 'field.runtime_max'
               for issue in verdict['diagnostics'])
    assert any('will be capped' in warning
               for warning in verdict['warnings'])


def test_artifact_unknown_format_preserves_warning_compatibility():
    verdict = validate_definition(_definition('artifact', {
        'path': 'report.bin',
        'format': 'future-format',
    }))
    assert verdict['ok'] is True
    assert any("param 'format' must be one of" in warning
               for warning in verdict['warnings'])


def test_exported_value_sets_cannot_drift_from_field_specs():
    def choices(kind, key):
        field = next(spec for spec in control_param_schema(kind)
                     if spec['key'] == key)
        return {option['value'] for option in field['options']}

    assert choices('artifact', 'format') == VALID_ARTIFACT_FORMATS
    assert choices('human', 'mode') == VALID_HUMAN_MODES


def test_unimplemented_loop_stop_strategies_are_not_new_authoring_choices():
    stop = next(spec for spec in control_param_schema('loop')
                if spec['key'] == 'stop_condition')
    options = {option['value']: option for option in stop['options']}
    assert options['verdict:STOP'].get('disabled') is not True
    assert options['no_new_findings']['disabled'] is True
    assert options['max_only']['disabled'] is True


def test_control_contract_has_one_physical_backend_owner():
    specs = (ROOT / 'lib/orchestration/_control_specs.py').read_text()
    validator = (ROOT / 'lib/orchestration/_validate.py').read_text()
    defaults = (ROOT / 'lib/orchestration/_defaults.py').read_text()
    authoring = (ROOT / 'lib/orchestration/authoring_contract.py').read_text()
    field_values = (ROOT / 'lib/orchestration/field_values.py').read_text()
    definition_schema = (
        ROOT / 'lib/orchestration/definition_contract_schema.py').read_text()

    assert 'CONTROL_PARAM_SCHEMA = {' in specs
    assert 'CONTROL_PARAM_SCHEMA = {' not in validator
    assert 'CONTROL_NODE_DEFAULTS = {' in specs
    assert '_CONTROL_NODE_DEFAULTS' not in defaults
    assert 'from lib.orchestration._control_specs import CONTROL_NODE_DEFAULTS' \
        in defaults
    assert 'from lib.orchestration._control_specs import CONTROL_KINDS' \
        in authoring
    assert 'from lib.orchestration._control_specs import CONTROL_KINDS' \
        in definition_schema
    assert 'from lib.orchestration._control_specs import control_param_schema' \
        in field_values
    assert 'def resolve_control_param(' in specs
    assert 'from lib.orchestration._validate import control_param_schema' \
        not in field_values
    assert specs.count('\n') < 190
    assert validator.count('\n') < 520

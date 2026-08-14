"""Canonical new-node param constructors shared by contract and built-ins."""

from __future__ import annotations

import pytest

from lib.orchestration import (
    CONTROL_KINDS,
    CONTROL_PARAM_SCHEMA,
    DEFAULT_HUMAN_APPROVAL_TIMEOUT,
    MAX_ARTIFACT_PATH_LEN,
    MAX_OBJECTIVE_LEN,
    all_control_node_params,
    build_adversarial_definition,
    build_autopilot_definition,
    build_blank_definition,
    build_endpoint_definition,
    build_fanout_definition,
    control_node_params,
    node_authoring_params,
    node_runtime_defaults,
    resolve_node_runtime_param,
    role_node_params,
    subflow_node_params,
)


pytestmark = pytest.mark.unit


def _node(definition: dict, node_id: str) -> dict:
    return next(node for node in definition['nodes'] if node['id'] == node_id)


def test_control_defaults_cover_validator_kinds_and_are_detached():
    defaults = all_control_node_params()
    assert set(defaults) == set(CONTROL_KINDS)
    assert defaults['loop'] == {
        'max_iterations': 10,
        'stop_condition': 'verdict:STOP',
        'verifier': 'critic',
    }
    assert defaults['human']['timeout_sec'] == \
        DEFAULT_HUMAN_APPROVAL_TIMEOUT == 300

    defaults['loop']['max_iterations'] = 999
    assert control_node_params('loop')['max_iterations'] == 10
    with pytest.raises(ValueError, match='unknown orchestration control kind'):
        control_node_params('future-control')


def test_control_field_specs_publish_validator_text_limits():
    text_specs = {
        (kind, spec['key']): spec
        for kind, fields in CONTROL_PARAM_SCHEMA.items()
        for spec in fields
        if spec['kind'] in {'text', 'textarea'}
    }
    assert text_specs[('start', 'seed')]['maxLength'] == MAX_OBJECTIVE_LEN
    assert text_specs[('human', 'prompt')]['maxLength'] == MAX_OBJECTIVE_LEN
    assert text_specs[('artifact', 'description')][
        'maxLength'] == MAX_OBJECTIVE_LEN
    assert text_specs[('artifact', 'path')][
        'maxLength'] == MAX_ARTIFACT_PATH_LEN


def test_role_and_subflow_defaults_are_detached_and_overridable():
    role = role_node_params(tier='heavy', objective='Ship')
    group = subflow_node_params(scope='inline', definition={'nodes': []})
    assert role == {
        'objective': 'Ship', 'tier': 'heavy', 'isolation': 'fresh-context',
    }
    assert group == {'scope': 'inline', 'definition': {'nodes': []}}

    role['objective'] = 'mutated'
    group['definition']['nodes'].append({})
    assert role_node_params()['objective'] == ''
    assert subflow_node_params() == {'scope': 'isolated'}


def test_node_authoring_params_is_the_uniform_dynamic_interface():
    role = node_authoring_params('role', tier='heavy', objective='Ship')
    control = node_authoring_params(
        'control', kind='loop', max_iterations=17)
    subflow = node_authoring_params('subflow', scope='inline')

    assert role == role_node_params(tier='heavy', objective='Ship')
    assert control == control_node_params('loop', max_iterations=17)
    assert subflow == subflow_node_params(scope='inline')
    control['max_iterations'] = 999
    assert node_authoring_params(
        'control', kind='loop')['max_iterations'] == 10
    with pytest.raises(ValueError, match='unknown orchestration node type'):
        node_authoring_params('future-node')
    with pytest.raises(ValueError, match='unknown orchestration control kind'):
        node_authoring_params('control')


def test_runtime_defaults_are_resolver_derived_and_distinct_from_authoring():
    defaults = node_runtime_defaults()
    assert defaults == {
        'role': {
            'tier': 'standard',
            'isolation': 'fresh-context',
        },
        'controls': {
            'loop': {
                'max_iterations': 10,
                'stop_condition': 'verdict:STOP',
            },
            'human': {'mode': 'approve', 'timeout_sec': 300},
        },
        'subflow': {'scope': 'inline'},
    }
    assert defaults['subflow'] != subflow_node_params()
    defaults['controls']['loop']['max_iterations'] = 999
    assert node_runtime_defaults()['controls']['loop']['max_iterations'] == 10


def test_runtime_param_resolver_is_the_uniform_dynamic_interface():
    assert resolve_node_runtime_param({
        'type': 'role', 'role': 'critic', 'params': {},
    }, 'emits') == 'user'
    assert resolve_node_runtime_param({
        'type': 'role', 'params': {'tier': 'heavy'},
    }, 'tier') == 'heavy'
    assert resolve_node_runtime_param({
        'type': 'control', 'kind': 'loop', 'params': {},
    }, 'max_iterations') == 10
    assert resolve_node_runtime_param({
        'kind': 'human', 'params': {},
    }, 'mode') == 'approve'
    assert resolve_node_runtime_param({'params': {}}, 'timeout_sec') == 300
    assert resolve_node_runtime_param({
        'type': 'subflow', 'params': {},
    }, 'scope') == 'inline'
    assert resolve_node_runtime_param({
        'type': 'role', 'params': {'future': ['value']},
    }, 'future') == ['value']


@pytest.mark.parametrize(
    ('node', 'key', 'expected'),
    [
        ({'type': 'role', 'params': ['malformed']}, 'tier', 'standard'),
        ({'type': 'role', 'params': 'malformed'},
         'isolation', 'fresh-context'),
        ({'type': 'role', 'role': 'critic', 'params': 7}, 'emits', 'user'),
        ({'type': 'subflow', 'params': ['malformed']}, 'scope', 'inline'),
        (None, 'tier', None),
    ],
)
def test_runtime_param_resolver_is_total_for_malformed_legacy_params(
    node, key, expected,
):
    assert resolve_node_runtime_param(node, key) == expected


def test_builtin_loops_reuse_defaults_with_intentional_overrides():
    endpoint = _node(build_endpoint_definition(max_iterations=7), 'loop')
    autopilot = _node(build_autopilot_definition(max_iterations=13), 'loop')

    assert endpoint['params'] == control_node_params(
        'loop', max_iterations=7, verifier='critic',
    )
    assert autopilot['params'] == control_node_params(
        'loop', max_iterations=13, verifier='virtual_user',
    )


@pytest.mark.parametrize(
    'builder',
    [build_endpoint_definition, build_autopilot_definition,
     build_fanout_definition, build_adversarial_definition],
)
def test_runnable_builtin_definitions_are_valid_laid_out_and_detached(builder):
    from lib.orchestration import validate_definition

    definition = builder()
    assert validate_definition(definition)['ok'] is True
    assert all('pos' in node for node in definition['nodes'])
    definition['nodes'][0]['id'] = 'client-mutation'
    assert builder()['nodes'][0]['id'] == 'start'


def test_blank_builtin_is_an_intentional_empty_draft():
    blank = build_blank_definition()
    assert blank == {
        'schema': 'tofu.orchestration/v1',
        'name': 'Untitled Flow',
        'nodes': [],
        'edges': [],
    }

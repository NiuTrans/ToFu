"""Exercise lifecycle retention at the shared final provider-wire boundary."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from lib.llm._sse_core import prepare_request
from lib.model_info._openai_gpt56 import GPT56_ALIAS_TARGET
from lib.tools.poll import KILL_TOOL, POLL_TOOL

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _without_cache_projection(monkeypatch):
    # Cache markers are transport metadata, not part of canonical schemas.
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints',
                        lambda *args, **kwargs: None)


def _fn(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': 'Optional read-only lookup.',
        'parameters': {'type': 'object', 'properties': {}},
    }}


def _body(controls=(POLL_TOOL, KILL_TOOL), *, stale_wire=False):
    optional = [_fn('lookup_' + str(i)) for i in range(18)]
    # Legacy searchable metadata is intentional: a latched/routed surface
    # must preserve live controls even though today's registry is eager.
    authority = deepcopy(list(controls)) + optional
    wire = optional if stale_wire else deepcopy(authority)
    return {
        'model': 'kimi-k3',
        'messages': [{'role': 'user', 'content': 'Continue.'}],
        'tools': deepcopy(wire),
        '_tool_wire_catalog': deepcopy(wire),
        '_executable_tool_catalog': authority,
        '_tool_discovery_policy_by_name': {
            tool['function']['name']: 'searchable' for tool in authority},
        '_tool_namespace_by_name': {
            tool['function']['name']: 'local' for tool in authority},
        '_responses_feature_profile': 'openai',
        '_tool_search_mode': 'local',
        '_tool_schema_budget_tokens': 1,
    }


def _request(body, protocol='openai'):
    base_url = {
        'openai': 'https://api.moonshot.cn/v1',
        'responses': 'https://api.openai.com/v1',
        'anthropic': 'https://api.anthropic.com',
    }[protocol]
    return prepare_request(
        body, base_url=base_url, api_key='test-key', api_protocol=protocol)


def _functions(body):
    """Flatten native Responses namespaces without losing deferral fields."""
    functions = {}
    for tool in body.get('tools', []):
        if tool.get('type') == 'namespace':
            functions.update(_functions({'tools': tool.get('tools', [])}))
        elif isinstance(tool.get('function'), dict):
            functions[tool['function']['name']] = tool['function']
        elif tool.get('name'):
            functions[tool['name']] = tool
    return functions


def _assert_canonical(body, controls=(POLL_TOOL, KILL_TOOL)):
    functions = _functions(body)
    for canonical in controls:
        expected = canonical['function']
        actual = functions[expected['name']]
        assert not actual.get('defer_loading', False)
        assert actual['description'] == expected['description']
        parameters = actual.get('parameters', actual.get('input_schema'))
        assert json.dumps(parameters, ensure_ascii=False) == json.dumps(
            expected['parameters'], ensure_ascii=False)
    assert 'await_agents' not in functions
    assert 'spawn_agent' not in functions


@pytest.mark.parametrize('search_mode', ['local', 'off'])
@pytest.mark.parametrize('programmatic_mode,exposure,tier', [
    ('off', 'additive', 'program'),
    ('auto', 'additive', 'program'),
    ('auto', 'gateway_only', 'program'),
    ('auto', 'gateway_only', 'batch'),
])
@pytest.mark.parametrize('stale_wire', [False, True])
def test_wire_controls_survive_routing_ptc_and_tiny_budget(
        search_mode, programmatic_mode, exposure, tier, stale_wire):
    body = _body(stale_wire=stale_wire)
    body.update({
        '_tool_search_mode': search_mode,
        '_programmatic_tool_calling': programmatic_mode,
        '_programmatic_eligible_tools': ['lookup_0'],
        '_programmatic_exposure': exposure,
        '_programmatic_tier': tier,
    })
    before = deepcopy(body)
    plan = _request(body)
    assert body == before
    _assert_canonical(plan.body)
    names = set(_functions(plan.body))
    if programmatic_mode != 'off':
        assert plan.programmatic_backend == 'local'
        assert ('execute_program' if tier == 'program' else 'execute_tools') in names
    if search_mode == 'local' and exposure != 'gateway_only':
        assert 'search_tools' in names
    if search_mode == 'local' or exposure == 'gateway_only':
        assert 'lookup_17' not in names
    else:
        assert 'lookup_17' in names  # Search-off has no schema cost target.
    for canonical in (POLL_TOOL, KILL_TOOL):
        actual = next(tool for tool in plan.body['tools']
                      if tool.get('function', {}).get('name')
                      == canonical['function']['name'])
        assert json.dumps(actual, ensure_ascii=False) == json.dumps(
            canonical, ensure_ascii=False)


@pytest.mark.parametrize('protocol,model', [
    ('responses', GPT56_ALIAS_TARGET),
    ('anthropic', 'claude-opus-4-6'),
])
@pytest.mark.parametrize('stale_wire', [False, True])
def test_native_discovery_never_defers_controls(protocol, model, stale_wire):
    body = _body(stale_wire=stale_wire)
    body.update(model=model, _tool_search_mode='auto')
    plan = _request(body, protocol)
    expected_backend = ('native_openai' if protocol == 'responses'
                        else 'native_anthropic')
    assert plan.tool_search_backend == expected_backend
    _assert_canonical(plan.body)
    # Confirm native discovery was really used; this is not just a names test.
    assert any(tool.get('type', '').startswith('tool_search')
               for tool in plan.body['tools'])
    functions = _functions(plan.body)
    assert any(tool.get('defer_loading') for name, tool in functions.items()
               if name.startswith('lookup_'))


@pytest.mark.parametrize('protocol,model,search_mode', [
    ('openai', 'kimi-k3', 'local'),
    ('responses', GPT56_ALIAS_TARGET, 'auto'),
    ('anthropic', 'claude-opus-4-6', 'auto'),
])
@pytest.mark.parametrize('controls', [(), (POLL_TOOL,)])
def test_live_authority_wins_over_stale_control_schemas(
        protocol, model, search_mode, controls):
    body = _body(controls)
    body.update(model=model, _tool_search_mode=search_mode)
    # Emulate a previous authorized epoch followed by read-only/feature revoke.
    body['tools'] += deepcopy([POLL_TOOL, KILL_TOOL])
    body['_tool_wire_catalog'] += deepcopy([POLL_TOOL, KILL_TOOL])
    plan = _request(body, protocol)
    functions = _functions(plan.body)
    assert 'kill' not in functions
    if controls:
        _assert_canonical(plan.body, controls)
    else:
        assert 'poll' not in functions


def test_legacy_direct_request_and_empty_authority_are_distinct():
    body = _body()
    body.pop('_executable_tool_catalog')
    body.pop('_tool_wire_catalog')
    body['_programmatic_tool_calling'] = 'auto'
    body['_programmatic_eligible_tools'] = ['lookup_0']
    body['_programmatic_exposure'] = 'gateway_only'
    _assert_canonical(_request(body).body)
    body['_executable_tool_catalog'] = []
    assert not {'poll', 'kill'}.intersection(_functions(_request(body).body))


@pytest.mark.parametrize('native_exposure', ['routed', 'full'])
@pytest.mark.parametrize('lean', [False, True])
def test_real_assembly_reaches_gateway_only_wire_with_canonical_controls(
        native_exposure, lean):
    from lib.tools.registry import ToolContext, assemble_tool_list

    ctx = ToolContext(
        cfg={'tools.nativeExposure': native_exposure}, task_id='lifecycle-wire',
        project_path='', project_enabled=False, search_mode='off',
        search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
        browser_enabled=False, desktop_enabled=False, enabled_plugins=set(),
        lean=lean, messages=[{'role': 'user', 'content': 'Continue.'}],
    )
    surface, _ = assemble_tool_list(ctx)
    body = {
        'model': 'kimi-k3', 'messages': ctx.messages, 'tools': surface,
        '_tool_wire_catalog': surface,
        '_executable_tool_catalog': ctx.executable_tool_catalog,
        '_tool_discovery_policy_by_name': ctx.discovery_policy_by_name,
        '_frontend_selected_tool_names': list(ctx.frontend_selected_tool_names),
        '_tool_search_mode': 'local', '_tool_schema_budget_tokens': 1,
        '_programmatic_tool_calling': 'auto',
        '_programmatic_eligible_tools': ['read_files'],
        '_programmatic_exposure': 'gateway_only',
    }
    before = deepcopy(body)
    plan = _request(body)
    assert plan.programmatic_backend == 'local'
    assert body == before
    _assert_canonical(plan.body)
    assert set(_functions(plan.body)) == {'poll', 'kill', 'execute_tools', 'execute_program'}


def test_read_only_worker_authority_cannot_regain_stale_lifecycle_controls():
    from lib.swarm.routing import read_only_swarm_tools

    body = _body()
    body['_executable_tool_catalog'] = read_only_swarm_tools(
        {}, body['_executable_tool_catalog'])
    body.update({
        '_programmatic_tool_calling': 'auto',
        '_programmatic_eligible_tools': ['lookup_0'],
        '_programmatic_exposure': 'gateway_only',
    })
    plan = _request(body)
    assert plan.programmatic_backend == 'local'
    assert set(_functions(plan.body)) == {'execute_tools', 'execute_program'}


def test_gateway_only_hidden_count_does_not_count_persistent_controls():
    body = _body()
    decision = {}
    body.update({
        '_tool_search_mode': 'off',
        '_programmatic_tool_calling': 'auto',
        '_programmatic_eligible_tools': ['lookup_0'],
        '_programmatic_exposure': 'gateway_only',
        '_tool_orchestration_decision_sink': decision,
    })
    plan = prepare_request(
        body, base_url='https://api.moonshot.cn/v1', api_key='test-key',
        api_protocol='openai')
    _assert_canonical(plan.body)
    assert decision['programmaticHiddenDirectToolCount'] == 18
    assert set(_functions(plan.body)) == {'poll', 'kill', 'execute_program'}


@pytest.mark.parametrize('search_mode', ['off', 'auto'])
def test_native_ptc_keeps_background_controls_direct(search_mode):
    body = _body(stale_wire=True)
    body.update({
        'model': GPT56_ALIAS_TARGET,
        '_tool_search_mode': search_mode,
        '_programmatic_tool_calling': 'auto',
        '_programmatic_eligible_tools': ['lookup_0'],
        '_programmatic_exposure': 'gateway_only',
    })
    before = deepcopy(body)
    plan = _request(body, 'responses')
    assert plan.programmatic_backend == 'native_openai'
    _assert_canonical(plan.body)
    assert body == before


@pytest.mark.parametrize('storage_free', [False, True])
def test_runtime_gate_survives_local_swarm_projection_and_budget(storage_free):
    from lib.swarm.tools import SPAWN_AGENTS_TOOL
    from lib.tools.registry import ToolContext, assemble_tool_list

    ctx = ToolContext(
        cfg={'_storageFreeRuntime': storage_free}, task_id='runtime-lifecycle',
        project_path='', project_enabled=False, search_mode='off',
        search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
        browser_enabled=False, desktop_enabled=False, enabled_plugins=set(),
    )
    surface, _ = assemble_tool_list(ctx)
    body = {
        'model': 'claude-sonnet-4-6', 'tools': surface,
        'messages': [{'role': 'user', 'content': 'Continue.'}],
        '_executable_tool_catalog': ctx.executable_tool_catalog,
        '_tool_wire_catalog': surface,
        '_tool_discovery_policy_by_name': ctx.discovery_policy_by_name,
        '_tool_search_mode': 'local', '_tool_schema_budget_tokens': 1,
        '_multi_agent_mode': 'read_only', '_multi_agent_max_concurrent_agents': 2,
    }
    before = deepcopy(body)
    spawn_before = deepcopy(SPAWN_AGENTS_TOOL)
    plan = _request(body, 'anthropic')
    assert plan.multi_agent_backend == 'local_swarm'
    functions = _functions(plan.body)
    assert 'maxItems' not in functions['spawn_agents']['input_schema']['properties']['agents']
    assert 'await_agents' not in functions
    if storage_free:
        assert not {'poll', 'kill'}.intersection(functions)
    else:
        _assert_canonical(plan.body)
        assert 'get_agent_result' in functions
    assert body == before
    assert SPAWN_AGENTS_TOOL == spawn_before

"""Background advice must not invent controls absent from live task authority."""
from __future__ import annotations

from copy import deepcopy
import json
import threading

import pytest

from lib.llm._sse_core import prepare_request
from lib.model_info._openai_gpt56 import GPT56_ALIAS_TARGET
from lib.swarm.registry import scope_tools_for_role
from lib.tasks_pkg.background_commands import background_command_result
from lib.tasks_pkg.handlers import _background_command as handoff
from lib.tools.code_exec import CODE_EXEC_TOOL
from lib.tools.contracts import compile_execution_contract_documents
from lib.tools.gateway import (
    preserve_background_wire_tools,
    search_executable_catalog,
)
from lib.tools.poll import KILL_TOOL, POLL_TOOL
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

pytestmark = pytest.mark.unit

_CONTROL_SETS = [(), (POLL_TOOL,), (KILL_TOOL,), (POLL_TOOL, KILL_TOOL)]


def _catalog(controls=(), command_tool=PROJECT_TOOL_RUN_COMMAND):
    return deepcopy([command_tool, *controls])


def _names(catalog):
    return {tool['function']['name'] for tool in catalog}


def _assert_guidance(text, controls):
    assert ('`poll`' in text) == ('poll' in controls)
    assert ('`kill`' in text) == ('kill' in controls)
    if controls != {'poll', 'kill'}:
        assert 'parent or operator' in text
        assert 'Do not emulate unavailable controls' in text


@pytest.mark.parametrize('controls', _CONTROL_SETS)
@pytest.mark.parametrize('has_conversation', [False, True])
def test_receipt_uses_only_live_catalog(controls, has_conversation):
    task = {
        '_executable_tool_catalog': _catalog(controls),
        # Deliberately contradict authority with a stale fully privileged wire.
        '_tool_schema': _catalog((POLL_TOOL, KILL_TOOL)),
        '_swarm_role': 'general',
    }
    before = deepcopy(task)
    result = background_command_result(
        'unit-command', 'bg_unit', task=task,
        has_conversation=has_conversation)
    _assert_guidance(result, _names(controls))
    assert 'bg_unit' in result
    if has_conversation:
        assert 'end this turn for automatic delivery' in result
    else:
        assert 'No conversation completion delivery' in result
        assert 'will be shown in the conversation' not in result
    assert task == before


@pytest.mark.parametrize('authority', [None, [], 'invalid', [None, {}]])
def test_missing_or_invalid_authority_never_infers_controls(authority):
    task = {'_tool_schema': _catalog((POLL_TOOL, KILL_TOOL))}
    if authority is not None:
        task['_executable_tool_catalog'] = authority
    _assert_guidance(background_command_result(
        'unit-command', 'bg_unit', task=task), set())


def test_receipt_reads_revoked_catalog_at_render_time():
    task = {'_executable_tool_catalog': _catalog((POLL_TOOL, KILL_TOOL))}
    _assert_guidance(background_command_result(
        'unit-command', 'bg_unit', task=task), {'poll', 'kill'})
    task['_executable_tool_catalog'] = _catalog()
    _assert_guidance(background_command_result(
        'unit-command', 'bg_unit', task=task), set())


def _wire_functions(body):
    result = {}
    for tool in body.get('tools', []):
        if tool.get('type') == 'namespace':
            result.update(_wire_functions({'tools': tool.get('tools', [])}))
        else:
            fn = tool.get('function') if isinstance(tool.get('function'), dict) else tool
            if fn.get('name'):
                result[fn['name']] = fn
    return result


def _without_descriptions(value):
    if isinstance(value, dict):
        return {key: _without_descriptions(item) for key, item in value.items()
                if key != 'description'}
    if isinstance(value, list):
        return [_without_descriptions(item) for item in value]
    return value


@pytest.mark.parametrize('controls', _CONTROL_SETS)
@pytest.mark.parametrize('protocol,model,url', [
    ('openai', 'kimi-k3', 'https://api.moonshot.cn/v1'),
    ('responses', GPT56_ALIAS_TARGET, 'https://api.openai.com/v1'),
    ('anthropic', 'claude-opus-4-6', 'https://api.anthropic.com'),
])
@pytest.mark.parametrize('command_tool', [PROJECT_TOOL_RUN_COMMAND, CODE_EXEC_TOOL],
                         ids=['project', 'standalone'])
def test_final_provider_schema_projects_live_controls_without_granting_them(
        monkeypatch, controls, protocol, model, url, command_tool):
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints',
                        lambda *args, **kwargs: None)
    authority = _catalog(controls, command_tool)
    stale_wire = _catalog((POLL_TOOL, KILL_TOOL), command_tool)
    body = {
        'model': model,
        'messages': [{'role': 'user', 'content': 'Continue.'}],
        'tools': deepcopy(stale_wire),
        '_tool_wire_catalog': deepcopy(stale_wire),
        '_executable_tool_catalog': authority,
        '_responses_feature_profile': 'openai',
        '_tool_search_mode': 'off',
        '_tool_schema_budget_tokens': 1,
    }
    before = deepcopy(body)
    plan = prepare_request(body, base_url=url, api_key='test-key',
                           api_protocol=protocol)
    functions = _wire_functions(plan.body)
    expected_controls = _names(controls)
    assert set(functions) & {'poll', 'kill'} == expected_controls
    fn = functions['run_command']
    parameters = fn.get('parameters', fn.get('input_schema'))
    guidance = fn['description'] + parameters['properties']['background']['description']
    _assert_guidance(guidance, expected_controls)
    # Provider adapters may normalize constraints (e.g. Kimi's maxItems).
    # Compare against that provider's fully authorized baseline, not raw JSON.
    baseline_body = deepcopy(before)
    baseline_body['_executable_tool_catalog'] = _catalog(
        (POLL_TOOL, KILL_TOOL), command_tool)
    baseline = _wire_functions(prepare_request(
        baseline_body, base_url=url, api_key='test-key',
        api_protocol=protocol).body)['run_command']
    baseline_parameters = baseline.get('parameters', baseline.get('input_schema'))
    assert _without_descriptions(parameters) == _without_descriptions(baseline_parameters)
    assert parameters['properties']['working_dir'] == baseline_parameters['properties']['working_dir']
    if expected_controls == {'poll', 'kill'}:
        assert fn['description'] == command_tool['function']['description']
        assert parameters == baseline_parameters
    assert body == before


@pytest.mark.parametrize('controls', _CONTROL_SETS)
def test_discovery_projects_rich_help_without_mutating_contracts(controls):
    catalog = _catalog(controls)
    documents = compile_execution_contract_documents(catalog)
    before = deepcopy((catalog, documents))
    result = search_executable_catalog(
        catalog, 'run_command', contract_documents_by_name=documents)
    assert result['status'] == 'ok'
    item, = result['items']
    text = json.dumps(item)
    if 'kill' not in _names(controls):
        assert '`kill`' not in text
    if 'poll' not in _names(controls):
        assert '`poll`' not in text
    assert 'automatically' in item['arguments_schema']['properties']['background']['description']
    assert (catalog, documents) == before


@pytest.mark.parametrize('role,expected_controls', [
    ('reviewer', set()), ('coder', set()), ('analyst', set()),
    ('general', {'poll'}),
])
def test_actual_leaf_scope_never_restores_denied_controls(role, expected_controls):
    catalog = scope_tools_for_role(role, _catalog((POLL_TOOL, KILL_TOOL)))
    assert _names(catalog) == {'run_command', *expected_controls}
    task = {'_executable_tool_catalog': catalog}
    _assert_guidance(background_command_result(
        'unit-command', 'bg_unit', task=task), expected_controls)
    assert 'kill' not in _names(catalog)


def test_automatic_handoff_renders_current_parent_authority(monkeypatch):
    """A worker snapshot cannot retain control advice after parent revocation."""
    task = {'convId': 'unit-handoff', '_userId': 7,
            '_executable_tool_catalog': _catalog((POLL_TOOL, KILL_TOOL))}
    release = threading.Event()
    delivered = threading.Event()
    worker_done = []

    def execute(command_task):
        command_task['_subprocess_pid'] = 123  # Lifecycle marker, not a real PID.
        release.wait(3)
        return 'finished'

    def register(**kwargs):
        worker_done.append(kwargs['done'])
        task['_executable_tool_catalog'] = _catalog()
        return True

    monkeypatch.setattr('lib.agent_inbox.has_pending', lambda *args, **kwargs: True)
    monkeypatch.setattr('lib.tasks_pkg.background_commands.register_handed_off_command', register)
    monkeypatch.setattr('lib.tasks_pkg.background_commands.finish_handed_off_command',
                        lambda **kwargs: worker_done[0].set())
    monkeypatch.setattr('lib.tasks_pkg.background_commands.set_handed_off_delivery',
                        lambda *args: delivered.set())
    monkeypatch.setattr(handoff, '_queue_completion', lambda **kwargs: False)
    try:
        result = handoff.run_with_steer_handoff(
            task=task, config={}, command='unit-command', execute=execute)
        assert handoff.is_background_command_result(result)
        _assert_guidance(result, set())
    finally:
        release.set()
        assert delivered.wait(3)


def test_standalone_explicit_handler_forwards_live_task(monkeypatch):
    from lib.tasks_pkg.handlers import code_exec

    task = {'id': 'unit-task', 'convId': 'unit-conv', '_userId': 7,
            '_executable_tool_catalog': _catalog((POLL_TOOL, KILL_TOOL))}

    def start(**kwargs):
        assert kwargs['task'] is task
        task['_executable_tool_catalog'] = _catalog()
        return 'bg_unit'

    monkeypatch.setattr('lib.tasks_pkg.background_commands.start_background_command', start)
    monkeypatch.setattr(code_exec, '_finalize_tool_round', lambda *args, **kwargs: None)
    _, content, _ = code_exec._handle_code_exec(
        task, None, 'run_command', 'tc-unit',
        {'command': 'unit-command', 'background': True}, 1,
        {'toolName': 'code_exec', 'status': 'searching'}, {}, '/tmp', False)
    assert handoff.is_background_command_result(content)
    _assert_guidance(content, set())


@pytest.mark.parametrize('initial', _CONTROL_SETS)
@pytest.mark.parametrize('current', _CONTROL_SETS)
def test_latched_guidance_reprojects_for_authority_changes(initial, current):
    canonical = deepcopy(PROJECT_TOOL_RUN_COMMAND)
    wire = preserve_background_wire_tools(
        _catalog((POLL_TOOL, KILL_TOOL)), authority_catalog=_catalog(initial))
    previous = deepcopy(wire)
    projected = preserve_background_wire_tools(wire, authority_catalog=_catalog(current))
    assert _names(projected) == {'run_command', *_names(current)}
    fn = next(tool['function'] for tool in projected
              if tool['function']['name'] == 'run_command')
    text = fn['description'] + fn['parameters']['properties']['background']['description']
    _assert_guidance(text, _names(current))
    assert _without_descriptions(fn) == _without_descriptions(canonical['function'])
    if _names(current) == {'poll', 'kill'}:
        assert fn == canonical['function']
    assert wire == previous
    assert PROJECT_TOOL_RUN_COMMAND == canonical

"""Local Tool Search, direct execution, and hidden-adapter contracts."""

from __future__ import annotations

import json
import threading

import pytest

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm._sse_core import (
    activate_native_tool_search_fallback,
    prepare_request,
)
from lib.tasks_pkg.tool_dispatch import parse_tool_calls
from lib.tools.gateway import (
    EXECUTE_TOOLS_NAME,
    SEARCH_TOOLS_NAME,
    full_wire_tools,
    gateway_tool_schemas,
    local_wire_tools,
    normalize_execute_request,
    resolve_tool_search_backend,
    search_enabled_catalog,
)
from lib.tools.toolscript import ToolScriptError, execute_toolscript


pytestmark = pytest.mark.unit


def _tool(name, *, description='', properties=None, required=None):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description or name,
            'parameters': {
                'type': 'object',
                'properties': properties or {},
                'required': required or [],
                'additionalProperties': False,
            },
        },
    }


def _names(tools):
    return [str((tool.get('function') or {}).get('name') or tool.get('name') or '')
            for tool in tools or []]


def test_execute_normalizes_common_model_shapes_and_safe_scalar_types():
    catalog = [_tool(
        'scale', properties={
            'count': {'type': 'integer'},
            'enabled': {'type': 'boolean'},
        }, required=['count'])]
    result = normalize_execute_request(
        {'calls': {'tool': 'scale',
                   'args': '{"count":"3","enabled":"false"}'}},
        catalog=catalog, namespace_by_name={'scale': 'math'},
        gateway_call_id='outer-1')

    assert result['errors'] == []
    assert len(result['calls']) == 1
    call = result['calls'][0]
    assert call['function']['name'] == 'scale'
    assert call['_normalized_arguments'] == {'count': 3, 'enabled': False}
    assert [repair['kind'] for repair in call['_normalization_repairs']] == [
        'string_to_integer', 'string_to_boolean']


def test_gateway_schema_teaches_search_execute_and_toolscript_precisely():
    schemas = gateway_tool_schemas()
    assert _names(schemas) == [SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]
    search = schemas[0]['function']
    execute = schemas[1]['function']
    assert 'This only finds tools' in search['description']
    assert 'call execute_tools' in search['description']
    assert set(execute['parameters']['properties']) == {
        'calls', 'execution', 'program'}
    assert execute['parameters']['properties']['calls']['maxItems'] == 16
    assert 'catalog.search' in execute['description']
    assert 'tools.parallel' in execute['description']
    assert 'no eval' in execute['description']


def test_search_result_names_the_stable_execution_gateway():
    result = search_enabled_catalog([_tool('read_doc')], 'read document')
    assert result['execute_with'] == EXECUTE_TOOLS_NAME
    assert result['notice'] == (
        "Call execute_tools with a result's exact name and arguments matching "
        'arguments_schema.')
    empty = search_enabled_catalog([], 'read document')
    assert empty['execute_with'] == EXECUTE_TOOLS_NAME
    assert empty['notice'] == result['notice']


def test_gateway_repairs_aliases_defaults_enums_arrays_and_top_level_call():
    catalog = [_tool(
        'repairable', properties={
            'mode': {'type': 'string', 'enum': ['dry_run']},
            'items': {'type': 'array', 'items': {'type': 'string'}},
            'enabled': {'type': 'boolean', 'default': True},
        }, required=['mode', 'items', 'enabled'])]
    result = normalize_execute_request(
        {'tool': 'repairable',
         'args': {'mode': 'DRY_RUN', 'items': 'one'}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='repair-1')

    assert result['errors'] == []
    assert result['warnings'][0]['code'] == 'wrapped_single_call'
    call = result['calls'][0]
    assert call['_normalized_arguments'] == {
        'mode': 'dry_run', 'items': ['one'], 'enabled': True}
    assert {repair['kind'] for repair in call['_normalization_repairs']} >= {
        'casefold_enum', 'scalar_to_array', 'schema_default'}

    alias_catalog = [_tool(
        'apply_diff', properties={
            'path': {'type': 'string'}, 'search': {'type': 'string'},
            'replace': {'type': 'string'}},
        required=['path', 'search', 'replace'])]
    aliased = normalize_execute_request(
        {'calls': {'name': 'apply_diff', 'arguments': {
            'file_path': 'a.py', 'old_string': 'x', 'new_string': 'y'}}},
        catalog=alias_catalog, namespace_by_name={}, gateway_call_id='repair-2')
    assert aliased['errors'] == []
    assert aliased['calls'][0]['_normalized_arguments'] == {
        'path': 'a.py', 'search': 'x', 'replace': 'y'}
    assert sum(row['kind'] == 'param_alias'
               for row in aliased['calls'][0]['_normalization_repairs']) == 3


def test_gateway_fuzzy_name_requires_high_confidence_and_clear_margin():
    repaired = normalize_execute_request(
        {'calls': {'name': 'read_fiels', 'arguments': {}}},
        catalog=[_tool('read_files'), _tool('write_file')],
        namespace_by_name={}, gateway_call_id='fuzzy-1')
    assert repaired['errors'] == []
    call = repaired['calls'][0]
    assert call['function']['name'] == 'read_files'
    repair = next(row for row in call['_normalization_repairs']
                  if row['kind'] == 'fuzzy_tool_name')
    assert repair['confidence'] >= 0.90
    assert repair['margin'] >= 0.15

    ambiguous = normalize_execute_request(
        {'calls': {'name': 'update_dco', 'arguments': {}}},
        catalog=[_tool('update_doc'), _tool('update_dog')],
        namespace_by_name={}, gateway_call_id='fuzzy-2')
    assert ambiguous['calls'] == []
    assert ambiguous['errors'][0]['code'] == 'tool_not_enabled'
    assert ambiguous['errors'][0]['candidates']
    assert 'retry_hint' in ambiguous['errors'][0]
    assert call['id'].startswith('gw_')


def test_execute_program_wins_with_warning_and_disabled_tool_is_rejected():
    catalog = [_tool('allowed')]
    both = normalize_execute_request(
        {'program': 'return 1;', 'calls': {'name': 'allowed'}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='outer-2')
    assert both['program'] == 'return 1;'
    assert both['calls'] == []
    assert both['warnings'][0]['code'] == 'program_preferred_over_calls'

    denied = normalize_execute_request(
        {'calls': {'name': 'not_in_catalog'}}, catalog=catalog,
        namespace_by_name={}, gateway_call_id='outer-3')
    assert denied['errors'][0]['code'] == 'tool_not_enabled'


def test_missing_or_semantic_argument_repairs_are_not_guessed():
    catalog = [_tool(
        'dangerous', properties={
            'mode': {'type': 'string', 'enum': ['dry_run', 'apply']},
            'count': {'type': 'integer'},
        }, required=['mode', 'count'])]
    missing = normalize_execute_request(
        {'calls': {'name': 'dangerous', 'arguments': {'count': 1}}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='g')
    assert missing['errors'][0]['code'] == 'missing_required_arguments'

    ambiguous = normalize_execute_request(
        {'calls': {'name': 'dangerous',
                   'arguments': {'mode': 'yes please', 'count': 'several'}}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='g')
    assert ambiguous['errors'][0]['code'] in {
        'invalid_argument_type', 'invalid_argument_value'}


def test_searchable_tool_not_on_wire_is_still_a_real_native_call():
    searchable = _tool('hidden_but_enabled')
    task = {
        'id': 'task_gateway_authority', 'convId': 'conv-gateway',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [_tool('visible')],
        '_enabled_tool_catalog': [searchable],
    }
    assistant = {'content': '', 'tool_calls': [{
        'id': 'native-1', 'type': 'function', 'source': 'native_direct',
        'function': {'name': 'hidden_but_enabled', 'arguments': '{}'},
    }]}
    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert len(parsed) == 1
    assert parsed[0][1] == 'hidden_but_enabled'
    assert parsed[0][6] is None
    assert parsed[0][5]['source'] == 'native_direct'
    assert parsed[0][5].get('status') != 'rejected'


def test_executable_catalog_name_is_the_preferred_direct_call_authority():
    executable = _tool('guessed_available_tool')
    task = {
        'id': 'task_executable_authority', 'convId': 'conv-executable',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [_tool('visible')],
        '_executable_tool_catalog': [executable],
        # A stale compatibility alias must not narrow the new authority.
        '_enabled_tool_catalog': [],
    }
    assistant = {'content': '', 'tool_calls': [{
        'id': 'native-executable-1', 'type': 'function',
        'function': {'name': 'guessed_available_tool', 'arguments': '{}'},
    }]}
    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert len(parsed) == 1
    assert parsed[0][1] == 'guessed_available_tool'
    assert parsed[0][5].get('status') != 'rejected'


def test_hidden_composer_tool_flows_from_assembly_to_direct_call_admission():
    from lib.tasks_pkg.model_config import _assemble_tool_list

    cfg = {
        'mcpEnabled': False,
        'tools': {'executionScope': 'available', 'toolSearch': 'local'},
    }
    wire, _ = _assemble_tool_list(
        cfg=cfg, project_path='', project_enabled=False,
        task_id='task-hidden-direct', search_mode='off',
        search_enabled=False, fetch_enabled=False,
        code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, swarm_enabled=False,
        image_gen_enabled=False, human_guidance_enabled=False,
        messages=[])
    assert 'run_command' not in _names(wire)
    assert 'run_command' in _names(cfg['_executableToolCatalog'])

    task = {
        'id': 'task-hidden-direct', 'convId': 'conv-hidden-direct',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': wire,
        '_executable_tool_catalog': cfg['_executableToolCatalog'],
    }
    assistant = {'content': '', 'tool_calls': [{
        'id': 'native-hidden-direct', 'type': 'function',
        'function': {'name': 'run_command',
                     'arguments': '{"command":"pwd"}'},
    }]}
    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert parsed[0][1] == 'run_command'
    assert parsed[0][5].get('status') != 'rejected'


def test_local_search_bridges_paraphrases_and_languages_without_embedding():
    catalog = [
        _tool('find_files', description='Find files by glob pattern.'),
        _tool('grep_search',
              description='Search file contents by regular expression.'),
        _tool('memory_write', description='Save a long-term memory.'),
        _tool('memory_search', description='Search long-term memories.'),
    ]
    code = search_enabled_catalog(
        catalog, 'find symbol references in code', limit=2)
    assert _names([{'function': {'name': row['name']}}
                   for row in code['items']])[0] == 'grep_search'

    memory = search_enabled_catalog(catalog, '找回之前拍板的决定', limit=2)
    assert memory['items'][0]['name'] == 'memory_search'


def test_private_search_hints_improve_ranking_but_never_leak_to_results():
    catalog = [
        _tool('project_message', description='Message a project peer.'),
        _tool('mcp__chat__post', description='Post to a channel.'),
    ]
    secret_hint = 'notify coworkers team group chat private-index-marker'
    result = search_enabled_catalog(
        catalog, 'tell everyone in the team chat', limit=2,
        search_text_by_name={'mcp__chat__post': secret_hint})

    assert result['items'][0]['name'] == 'mcp__chat__post'
    assert 'private-index-marker' not in str(result)


def test_builtin_specs_carry_per_function_private_search_hints():
    from lib.tools import all_specs

    by_key = {spec.key: spec for spec in all_specs()}
    assert 'symbol references' in by_key['project'].search_hints['grep_search']
    assert 'previous decision' in by_key['memory'].search_hints[
        'search_memories']
    assert 'cancel stop' in by_key['scheduler'].search_hints[
        'schedule_manage']


def test_explicitly_empty_authority_never_falls_back_to_latched_wire_schema():
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    task = {
        'id': 'task-empty-authority', 'convId': 'conv-empty-authority',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [_tool('read_files')],
        '_enabled_tool_catalog': [],
    }
    assert 'read_files' not in _known_tool_names(task)

    assistant = {'content': '', 'tool_calls': [{
        'id': 'disabled-1', 'type': 'function',
        'function': {'name': 'read_files', 'arguments': '{}'},
    }]}
    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert parsed[0][5]['status'] == 'rejected'
    assert parsed[0][5]['_rejected']['kind'] == 'hallucinated'


def test_model_wire_uses_fixed_gateway_only_when_local_discovery_is_needed():
    small = [_tool('read_doc'), _tool('update_doc')]
    assert _names(local_wire_tools(small)) == ['read_doc', 'update_doc']
    assert _names(full_wire_tools(small)) == [
        'read_doc', 'update_doc']

    large = [_tool(f'tool_{index}') for index in range(12)]
    policies = {f'tool_{index}': 'searchable' for index in range(12)}
    assert _names(local_wire_tools(
        large, discovery_policy_by_name=policies)) == [
            SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]

    assert _names(gateway_tool_schemas()) == [
        SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]
    assert gateway_tool_schemas(include_search=False) == []


def test_local_search_is_omitted_when_a_large_catalog_is_all_eager():
    catalog = [_tool(f'eager_{index}') for index in range(12)]
    policies = {f'eager_{index}': 'eager' for index in range(12)}

    assert _names(local_wire_tools(
        catalog, discovery_policy_by_name=policies,
        discovery_catalog_size=12, searchable_count=0)) == _names(catalog)


def test_small_latched_projection_keeps_search_for_large_authority_catalog():
    visible = [_tool('read_doc')]
    authority = visible + [_tool(f'hidden_{index}') for index in range(19)]
    body = {
        'model': 'qwen-test', 'messages': [{'role': 'user', 'content': 'work'}],
        'tools': visible, '_tool_wire_catalog': visible,
        '_enabled_tool_catalog': authority,
        '_tool_discovery_policy_by_name': {'read_doc': 'eager'},
        '_tool_search_catalog_size': 20, '_tool_searchable_count': 19,
        '_tool_search_mode': 'local',
    }

    plan = prepare_request(
        body, api_protocol='openai',
        base_url='https://compatible.example/v1')

    assert _names(plan.body['tools']) == [
        'read_doc', SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]


def test_provider_boundary_uses_latched_wire_catalog_not_live_authority():
    frozen = [_tool('visible')]
    authority = frozen + [_tool('enabled_but_hidden')]
    base = {
        'model': 'qwen-test', 'messages': [{'role': 'user', 'content': 'work'}],
        'tools': frozen, '_tool_wire_catalog': frozen,
        '_enabled_tool_catalog': authority,
        '_tool_search_mode': 'off',
    }

    plan = prepare_request(
        base, api_protocol='openai',
        base_url='https://compatible.example/v1')

    assert _names(plan.body['tools']) == ['visible']


def test_local_wire_bytes_ignore_live_authority_changes_after_latch():
    frozen = [_tool(f'tool_{index}') for index in range(12)]
    policy = {'tool_0': 'eager', **{
        f'tool_{index}': 'searchable' for index in range(1, 12)}}
    base = {
        'model': 'qwen-test', 'messages': [{'role': 'user', 'content': 'work'}],
        'tools': frozen, '_tool_wire_catalog': frozen,
        '_tool_discovery_policy_by_name': policy,
        '_tool_search_catalog_size': 12, '_tool_searchable_count': 11,
        '_tool_search_mode': 'local',
    }
    before = prepare_request(
        {**base, '_enabled_tool_catalog': frozen}, api_protocol='openai',
        base_url='https://compatible.example/v1')
    after = prepare_request(
        {**base, '_enabled_tool_catalog': frozen + [_tool('new_live_tool')]},
        api_protocol='openai', base_url='https://compatible.example/v1')

    assert before.body['tools'] == after.body['tools']
    assert _names(after.body['tools']) == [
        'tool_0', SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]


def test_hidden_execute_gateway_is_admitted_when_model_guesses_it():
    """The wrapper costs no schema tokens but a correctly shaped call works."""
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    task = {
        'id': 'task-no-execute-wrapper',
        '_enabled_tool_catalog': [_tool('read_doc')],
        '_tool_gateway_names': [EXECUTE_TOOLS_NAME, SEARCH_TOOLS_NAME],
    }
    assert EXECUTE_TOOLS_NAME in _known_tool_names(task)
    # Schema-less recovery also preserves the registered compatibility path.
    assert EXECUTE_TOOLS_NAME in _known_tool_names({'id': 'bare-task'})

    assistant = {'content': '', 'tool_calls': [{
        'id': 'new-wrapper-call', 'type': 'function',
        'function': {'name': EXECUTE_TOOLS_NAME, 'arguments': '{"calls":[]}'},
    }]}
    live = {
        **task, 'convId': 'conv-no-execute-wrapper', 'model': 'test',
        'events': [], 'events_lock': threading.Lock(), 'toolRounds': [],
    }
    parsed, _ = parse_tool_calls(
        assistant, live, round_num=0, tool_round_num=0,
        project_enabled=False)
    assert len(parsed) == 1
    assert parsed[0][6] is None
    assert parsed[0][5].get('status') != 'rejected'

    from lib.tools import all_specs
    gateway_spec = next(spec for spec in all_specs()
                        if spec.key == 'tool_gateway')
    assert gateway_spec.provides == frozenset({
        SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME})


def test_round_assembly_admits_hidden_execute_without_exposing_schema(
        monkeypatch):
    from lib.tasks_pkg.orchestrator import _tool_assembly_prep as prep

    monkeypatch.setattr(
        prep, '_assemble_tool_list',
        lambda *_args, **_kwargs: ([_tool('read_doc')], True))
    task = {'id': 'task-assembly', 'convId': 'conv-assembly',
            'messages': []}
    mcfg = {
        'project_path': '', 'project_enabled': False,
        'search_mode': 'multi', 'search_enabled': True,
        'fetch_enabled': True, 'code_exec_enabled': False,
        'browser_enabled': False, 'desktop_enabled': False,
        # True avoids involving the pending-swarm recovery seam; this test is
        # solely about gateway admission after ordinary tool assembly.
        'swarm_enabled': True, 'image_gen_enabled': False,
        'human_guidance_enabled': False, 'scheduler_enabled': False,
    }
    prep.assemble_round_tools({}, task, mcfg)

    assert task['_tool_gateway_names'] == [
        EXECUTE_TOOLS_NAME, SEARCH_TOOLS_NAME]


def test_execute_gateway_has_quiet_display(caplog):
    from lib.tasks_pkg.tool_display import tool_round_label

    caplog.clear()
    label = tool_round_label(EXECUTE_TOOLS_NAME, {'calls': [
        {'name': 'read_files'}, {'tool': 'grep_search'},
    ]})
    assert label == 'Tool batch: read_files, grep_search'
    assert not any('Unregistered tool execute_tools' in record.message
                   for record in caplog.records)


def test_guessed_execute_gateway_runs_normalized_children(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    seen = []

    def fake_execute(task, calls, execution, **kwargs):
        seen.extend(calls)
        return [{
            'call_id': calls[0]['id'], 'name': 'scale', 'status': 'done',
            'approval': {'required': False, 'status': 'not_required'},
            'duration': 1, 'source': 'execute_calls', 'output': '6',
        }]

    monkeypatch.setattr(handler, '_execute_normalized', fake_execute)
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'model': 'test', '_enabled_tool_catalog': [_tool(
            'scale', properties={'count': {'type': 'integer'}},
            required=['count'])],
        '_enabledToolNamespaceByName': {},
    }
    tc_id, content, aborted = handler.handle_execute_tools(
        task, {}, EXECUTE_TOOLS_NAME, 'guessed-wrapper-1',
        {'calls': {'tool': 'scale', 'args': {'count': '3'}}},
        1, {}, {}, None, False)

    assert tc_id == 'guessed-wrapper-1'
    assert aborted is False
    assert '"status":"ok"' in content
    assert seen[0]['function']['name'] == 'scale'
    assert seen[0]['_normalized_arguments'] == {'count': 3}


def test_execute_program_can_search_then_call_without_wire_promotion(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    seen = []

    def fake_execute(task, calls, execution, **kwargs):
        seen.extend(calls)
        return [{
            'call_id': calls[0]['id'], 'name': 'scale', 'status': 'done',
            'approval': {'required': False, 'status': 'not_required'},
            'duration': 1, 'source': 'execute_program', 'output': '6',
        }]

    monkeypatch.setattr(handler, '_execute_normalized', fake_execute)
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'model': 'test', '_enabled_tool_catalog': [_tool(
            'scale', description='Scale a number.',
            properties={'count': {'type': 'integer'}}, required=['count'])],
        '_enabledToolNamespaceByName': {},
        '_enabledToolSearchTextByName': {},
    }
    _, content, aborted = handler.handle_execute_tools(
        task, {}, EXECUTE_TOOLS_NAME, 'program-wrapper-1', {
            'program': (
                'let found=catalog.search("scale"); '
                'return tools.call(found.items[0].name,{count:"3"});')},
        1, {'llmRound': 0}, {}, None, False)

    payload = json.loads(content)
    assert aborted is False
    assert payload['status'] == 'ok'
    assert payload['program']['result']['output'] == '6'
    assert seen[0]['function']['name'] == 'scale'
    assert seen[0]['_normalized_arguments'] == {'count': 3}
    assert task['programRuns'][0]['source'] == 'execute_program'


def test_gateway_repair_audit_keeps_argument_values_private(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    events = []
    monkeypatch.setattr(handler, 'audit_log',
                        lambda event, **detail: events.append((event, detail)))
    handler._audit_gateway_repairs({'model': 'test'}, {
        'function': {'name': 'write_file'},
        '_normalization_repairs': [
            {'path': '$.name', 'kind': 'fuzzy_tool_name',
             'before': 'write_flie', 'after': 'write_file',
             'confidence': 0.9},
            {'path': '$.arguments.content', 'kind': 'schema_default',
             'before': None, 'after': 'private-value'},
        ],
    })

    assert events[0][1]['attempted'] == 'write_flie'
    assert events[0][1]['resolved'] == 'write_file'
    assert 'before' not in events[1][1]
    assert 'after' not in events[1][1]
    assert 'private-value' not in str(events)


def test_fuzzy_repaired_write_still_enters_the_real_approval_gate(monkeypatch):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    from lib.tasks_pkg.tool_dispatch import _pipeline

    approvals = []

    def reject(task, name, args, *rest, **kwargs):
        approvals.append((name, dict(args)))
        return False, 'User rejected this write.'

    monkeypatch.setattr(_pipeline, '_handle_approval', reject)
    catalog = [_tool(
        'write_file', properties={
            'path': {'type': 'string'}, 'content': {'type': 'string'}},
        required=['path', 'content'])]
    normalized = normalize_execute_request(
        {'calls': {'name': 'write_flie', 'arguments': {
            'path': 'x.txt', 'content': 'danger'}}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='write-gw')
    assert normalized['errors'] == []
    assert normalized['calls'][0]['function']['name'] == 'write_file'

    task = {
        'id': 'gateway-write-approval', 'convId': 'gateway-write-conv',
        'status': 'running', 'aborted': False, 'model': 'test', 'events': [],
        'events_lock': threading.Lock(), '_attended': True,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, 'toolRounds': [],
        '_enabled_tool_catalog': catalog,
    }
    result = _execute_call_batch(
        task, normalized['calls'], cfg={'autoApply': False},
        project_path=None, project_enabled=False, model='test', llm_round=0)

    assert approvals == [('write_file', {'path': 'x.txt',
                                         'content': 'danger'})]
    assert result[0]['status'] == 'rejected'
    assert result[0]['error'] == 'User rejected this write.'


def test_provider_boundary_injects_execute_only_for_local_search():
    catalog = [_tool(f'tool_{index}') for index in range(12)]
    base = {
        'model': 'qwen-test',
        'messages': [{'role': 'user', 'content': 'find a tool'}],
        'tools': catalog,
        '_enabled_tool_catalog': catalog,
        '_tool_discovery_policy_by_name': {
            f'tool_{index}': 'searchable' for index in range(12)},
    }
    local = prepare_request(
        {**base, '_tool_search_mode': 'local'}, api_protocol='openai',
        base_url='https://compatible.example/v1')
    assert _names(local.body['tools']) == [
        SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]

    full = prepare_request(
        {**base, '_tool_search_mode': 'off'}, api_protocol='openai',
        base_url='https://compatible.example/v1')
    assert _names(full.body['tools']) == _names(catalog)
    assert EXECUTE_TOOLS_NAME not in _names(full.body['tools'])


def test_provider_strategy_is_fail_closed_for_unverified_endpoints():
    assert resolve_tool_search_backend(
        'auto', protocol='responses', model='gpt-5.6-sol',
        responses_profile='openai', base_url='https://api.openai.com/v1') \
        == 'native_openai'
    assert resolve_tool_search_backend(
        'auto', protocol='responses', model='gpt-5.6-sol',
        responses_profile='openai', base_url='https://proxy.example/v1') \
        == 'local'
    assert resolve_tool_search_backend(
        'native', protocol='anthropic', model='claude-opus-4-1',
        base_url='https://api.anthropic.com') == 'local'
    assert resolve_tool_search_backend(
        'auto', protocol='anthropic', model='claude-sonnet-4-6',
        base_url='https://api.anthropic.com') == 'native_anthropic'
    assert resolve_tool_search_backend(
        'auto', protocol='openai', model='qwen-next',
        base_url='https://gateway.example/v1',
        capabilities={'openai_native_tool_search': True}) == 'native_openai'


def test_native_search_rejection_downgrades_only_explicit_shape_errors():
    plan = type('Plan', (), {'tool_search_backend': 'native_anthropic'})()
    body = {}
    assert activate_native_tool_search_fallback(
        400, 'unknown field defer_loading', plan=plan,
        canonical_body=body)
    assert body['_force_local_tool_search'] is True

    body = {}
    assert not activate_native_tool_search_fallback(
        400, 'invalid max_tokens', plan=plan, canonical_body=body)
    assert '_force_local_tool_search' not in body
    local_plan = type('Plan', (), {'tool_search_backend': 'local'})()
    assert not activate_native_tool_search_fallback(
        400, 'unknown tool_search field', plan=local_plan,
        canonical_body={})


def test_anthropic_native_search_defers_only_searchable_unpinned_tools():
    tools = [_tool('always')] + [
        _tool(f'later_{index}') for index in range(11)]
    body = {
        'model': 'claude-sonnet-4-6', 'messages': [],
        'tools': tools,
        '_anthropic_native_tool_search': True,
        '_tool_discovery_policy_by_name': {
            'always': 'eager',
            **{f'later_{index}': 'searchable' for index in range(11)}},
        '_frontend_selected_tool_names': [],
    }
    wire = openai_body_to_anthropic(body)
    assert wire['tools'][0] == {
        'type': 'tool_search_tool_bm25_20251119',
        'name': 'tool_search_tool_bm25'}
    by_name = {tool['name']: tool for tool in wire['tools'][1:]}
    assert 'defer_loading' not in by_name['always']
    assert all(by_name[f'later_{index}']['defer_loading'] is True
               for index in range(11))
    assert EXECUTE_TOOLS_NAME not in by_name


def test_anthropic_native_search_skips_small_or_all_eager_catalogs():
    small = [_tool('always'), _tool('later')]
    small_wire = openai_body_to_anthropic({
        'model': 'claude-sonnet-4-6', 'messages': [], 'tools': small,
        '_anthropic_native_tool_search': True,
        '_tool_discovery_policy_by_name': {
            'always': 'eager', 'later': 'searchable'},
    })
    assert _names(small_wire['tools']) == ['always', 'later']

    eager = [_tool(f'eager_{index}') for index in range(12)]
    eager_wire = openai_body_to_anthropic({
        'model': 'claude-sonnet-4-6', 'messages': [], 'tools': eager,
        '_anthropic_native_tool_search': True,
        '_tool_discovery_policy_by_name': {
            f'eager_{index}': 'eager' for index in range(12)},
    })
    assert _names(eager_wire['tools']) == _names(eager)


def test_search_results_project_to_rich_ui_cards_without_private_schema():
    from lib.tasks_pkg.handlers.tool_gateway import _search_display_results

    cards = _search_display_results({'items': [{
        'name': 'mcp__xuecheng__update_doc',
        'namespace': 'xuecheng',
        'description': 'Update a Xuecheng document.',
        'score': 4.2,
        'arguments_schema': {
            'type': 'object',
            'properties': {
                'doc_id': {'type': 'string'},
                'confirm': {'type': ['boolean', 'null']},
            },
            'required': ['doc_id'],
        },
    }]})

    assert cards == [{
        'type': 'tool_catalog_match',
        'toolName': 'mcp__xuecheng__update_doc',
        'title': 'mcp__xuecheng__update_doc',
        'namespace': 'xuecheng',
        'snippet': 'Update a Xuecheng document.',
        'arguments': [
            {'name': 'doc_id', 'type': 'string', 'required': True},
            {'name': 'confirm', 'type': 'boolean', 'required': False},
        ],
        'score': 4.2,
    }]
    assert 'arguments_schema' not in cards[0]


def test_toolscript_data_flow_calls_and_security_limits():
    calls = []

    def call(name, args, call_id=None):
        calls.append((name, args, call_id))
        return {'status': 'done', 'output': args['value'] * 2}

    value, stats = execute_toolscript(
        'let xs=[1,2,3]; '
        'let ys=xs.map(x => x*2).filter(x => x>2); '
        'let r=tools.call("double", {value: ys.length}); '
        'return {sum:ys.reduce((a,b)=>a+b,0), result:r.output};',
        search=lambda *args: [], call=call, call_many=lambda *args: [])
    assert value == {'sum': 10, 'result': 4}
    assert calls == [('double', {'value': 2}, None)]
    assert stats['tool_calls'] == 1

    with pytest.raises(ToolScriptError, match='forbidden') as unsafe:
        execute_toolscript(
            'return ({safe: 1}).constructor;', search=lambda *args: [],
            call=call, call_many=lambda *args: [])
    assert unsafe.value.code == 'unsafe_member'

    with pytest.raises(ToolScriptError) as nested:
        execute_toolscript(
            'return ' + '[' * 40 + '1' + ']' * 40 + ';',
            search=lambda *args: [], call=call,
            call_many=lambda *args: [])
    assert nested.value.code == 'nesting_limit'


def test_shared_pipeline_replays_call_id_and_rejects_conflicting_reuse(
        monkeypatch):
    from lib.tasks_pkg.tool_dispatch import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append((fn_name, dict(fn_args)))
        return tc_id, f'ran:{fn_args.get("value")}', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)

    task = {
        'id': 'call-id-task', 'convId': 'call-id-conv', 'status': 'running',
        'aborted': False, 'model': 'test', 'events': [],
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }

    def parsed(value, seq):
        _, row, _ = _build_tool_round_entry(
            'side_effect_tool', {'value': value}, 'stable-call-id',
            '{"value":%d}' % value, seq, False)
        task['toolRounds'].append(row)
        tc = {'id': 'stable-call-id', 'type': 'function',
              'function': {'name': 'side_effect_tool',
                           'arguments': '{"value":%d}' % value}}
        return (tc, 'side_effect_tool', 'stable-call-id', {'value': value},
                row['roundNum'], row, None)

    def run(item):
        messages = []
        execute_tool_pipeline(
            task, [item], cfg={'autoApply': True}, project_path=None,
            project_enabled=False, tool_list=[], messages=messages,
            all_search_results_text=[], round_num=0, model='test')
        return messages, item[5]

    first_messages, first_row = run(parsed(1, 1))
    replay_messages, replay_row = run(parsed(1, 2))
    conflict_messages, conflict_row = run(parsed(2, 3))

    assert executions == [('side_effect_tool', {'value': 1})]
    assert first_messages[-1]['content'] == 'ran:1'
    assert replay_messages[-1]['content'] == 'ran:1'
    assert replay_row['_idempotentReplay'] is True
    assert conflict_row['status'] == 'rejected'
    assert 'already used' in conflict_messages[-1]['content']


def test_execute_gateway_children_do_not_bypass_call_id_conflicts(monkeypatch):
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    from lib.tasks_pkg.tool_dispatch import _heartbeat, _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append(dict(fn_args))
        return tc_id, f'ran:{fn_args["value"]}', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    task = {
        'id': 'gateway-child-id', 'convId': 'gateway-child-conv',
        'status': 'running', 'aborted': False, 'model': 'test', 'events': [],
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, 'toolRounds': [],
        '_enabled_tool_catalog': [_tool(
            'side_effect_tool', properties={'value': {'type': 'integer'}},
            required=['value'])],
    }

    def child(value):
        return {
            'id': 'explicit-child-id', 'type': 'function',
            'source': 'execute_calls', '_normalized_arguments': {'value': value},
            'function': {'name': 'side_effect_tool',
                         'arguments': '{"value":%d}' % value},
        }

    common = {
        'cfg': {'autoApply': True}, 'project_path': None,
        'project_enabled': False, 'model': 'test', 'llm_round': 0,
    }
    first = _execute_call_batch(task, [child(1)], **common)
    conflict = _execute_call_batch(task, [child(2)], **common)

    assert executions == [{'value': 1}]
    assert first[0]['output'] == 'ran:1'
    assert conflict[0]['status'] == 'rejected'
    assert 'already used' in conflict[0]['error']

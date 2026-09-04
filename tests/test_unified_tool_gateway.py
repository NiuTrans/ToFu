"""Local Tool Search, direct execution, and hidden-adapter contracts."""

from __future__ import annotations

import json
import threading

import pytest
import lib.tools.gateway as tool_gateway

from lib.llm.anthropic_outbound import openai_body_to_anthropic
from lib.llm._sse_core import (
    activate_native_tool_search_fallback,
    prepare_request,
)
from lib.tasks_pkg.tool_dispatch.api import parse_tool_calls
from lib.tools.gateway import (
    EXECUTE_TOOLS_NAME,
    SEARCH_TOOLS_NAME,
    full_wire_tools,
    gateway_tool_schemas,
    local_wire_tools,
    normalize_execute_request,
    resolve_tool_search_backend,
    search_executable_catalog,
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
    search_properties = search['parameters']['properties']
    assert search_properties['query']['maxLength'] == 512
    assert search_properties['namespace']['maxLength'] == 128
    assert search_properties['cursor']['maxLength'] == 128
    assert set(execute['parameters']['properties']) == {
        'calls', 'execution', 'program'}
    assert execute['parameters']['properties']['calls']['maxItems'] == 16
    assert 'search_tools is optional' in execute['description']
    assert 'data-dependent calls' in execute['description']
    assert 'choose one lane per action' in execute['description']
    assert 'catalog.search' in execute['description']
    assert 'tools.parallel' in execute['description']
    assert 'no eval' in execute['description']


def test_search_result_names_the_stable_execution_gateway():
    result = search_executable_catalog([_tool('read_doc')], 'read document')
    assert result['execute_with'] == EXECUTE_TOOLS_NAME
    assert result['notice'] == (
        "Call execute_tools with a result's exact name and arguments matching "
        'arguments_schema.')
    empty = search_executable_catalog([], 'read document')
    assert empty['execute_with'] == EXECUTE_TOOLS_NAME
    assert empty['notice'] == result['notice']


def test_search_rejects_oversized_address_fields_before_catalog_work():
    query = search_executable_catalog(
        [_tool('read_doc')],
        'q' * (tool_gateway.LOCAL_TOOL_SEARCH_MAX_QUERY_CHARS + 1),
    )
    assert query['error'] == {
        'code': 'invalid_query',
        'message': 'query exceeds the Tool Search character limit',
        'max_chars': tool_gateway.LOCAL_TOOL_SEARCH_MAX_QUERY_CHARS,
    }

    namespace = search_executable_catalog(
        [_tool('read_doc')],
        'read',
        namespace=(
            'n' * (tool_gateway.LOCAL_TOOL_SEARCH_MAX_NAMESPACE_CHARS + 1)
        ),
    )
    assert namespace['error']['code'] == 'invalid_namespace'

    cursor = search_executable_catalog(
        [_tool('read_doc')],
        'read',
        cursor='c' * (tool_gateway.LOCAL_TOOL_SEARCH_MAX_CURSOR_CHARS + 1),
    )
    assert cursor['error']['code'] == 'invalid_cursor'


def test_term_cache_hits_short_hot_set_but_never_retains_long_input():
    tool_gateway._terms_cached.cache_clear()
    try:
        short_text = 'read project files'
        assert 'project' in tool_gateway._terms(short_text)
        after_first = tool_gateway._terms_cached.cache_info()
        assert 'project' in tool_gateway._terms(short_text)
        after_second = tool_gateway._terms_cached.cache_info()
        assert after_second.hits == after_first.hits + 1

        long_text = 'private-catalog-term ' * 80
        assert len(long_text) > (
            tool_gateway.LOCAL_TOOL_SEARCH_TERM_CACHE_MAX_INPUT_CHARS)
        before_long = tool_gateway._terms_cached.cache_info()
        assert 'private-catalog-term' in tool_gateway._terms(long_text)
        assert 'private-catalog-term' in tool_gateway._terms(long_text)
        after_long = tool_gateway._terms_cached.cache_info()
        assert after_long == before_long
    finally:
        tool_gateway._terms_cached.cache_clear()


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


def test_gateway_repairs_tool_result_reader_name_and_ref_key_together():
    catalog = [_tool(
        'read_tool_artifact',
        properties={'artifact_ref': {'type': 'string'}},
        required=['artifact_ref'],
    )]
    result = normalize_execute_request(
        {'calls': {'name': 'read_artifact', 'arguments': {'ref': 'result-1'}}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='artifact-1')

    assert result['errors'] == []
    call = result['calls'][0]
    assert call['function']['name'] == 'read_tool_artifact'
    assert call['_normalized_arguments'] == {'artifact_ref': 'result-1'}
    assert {repair['kind'] for repair in call['_normalization_repairs']} >= {
        'alias_tool_name', 'param_alias'}


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


def test_gateway_final_validation_uses_request_contract_not_catalog_drift():
    from lib.tools.contracts import adapt_legacy_tool_contract

    catalog = [_tool(
        'read_batch', properties={
            'ids': {'type': 'array', 'items': {'type': 'string'}},
        }, required=['ids'])]
    authoritative_schema = _tool(
        'read_batch', properties={
            'ids': {'type': 'array', 'minItems': 1, 'maxItems': 2,
                    'items': {'type': 'string', 'minLength': 1}},
        }, required=['ids'])
    document = adapt_legacy_tool_contract(
        authoritative_schema).search_document()

    result = normalize_execute_request(
        {'calls': {'name': 'read_batch', 'arguments': {'ids': []}}},
        catalog=catalog, namespace_by_name={}, gateway_call_id='contract-1',
        contract_documents_by_name={'read_batch': document})

    assert result['calls'] == []
    assert result['errors'][0]['code'] == 'too_few_items'
    assert result['errors'][0]['path'] == '$.ids'
    assert result['errors'][0]['retryable'] is True
    assert result['errors'][0]['retry_hint']


def test_searchable_tool_not_on_wire_is_still_a_real_native_call():
    searchable = _tool('hidden_but_enabled')
    task = {
        'id': 'task_gateway_authority', 'convId': 'conv-gateway',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [_tool('visible')],
        '_executable_tool_catalog': [searchable],
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


def test_direct_dispatch_fails_closed_on_request_contract_violation():
    from lib.tools.contracts import adapt_legacy_tool_contract

    executable = _tool(
        'guarded_read', properties={
            'path': {'type': 'string', 'minLength': 3, 'maxLength': 20},
        }, required=['path'])
    document = adapt_legacy_tool_contract(executable).search_document()
    task = {
        'id': 'task_contract_authority', 'convId': 'conv-contract',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [executable],
        '_executable_tool_catalog': [executable],
        '_toolContractDocumentsByName': {'guarded_read': document},
    }
    assistant = {'content': '', 'tool_calls': [{
        'id': 'contract-direct-1', 'type': 'function',
        'function': {'name': 'guarded_read',
                     'arguments': json.dumps({'path': '.'})},
    }]}

    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert len(parsed) == 1
    assert '[invalid_argument_length]' in parsed[0][6]
    assert parsed[0][5]['status'] == 'rejected'
    assert parsed[0][5]['_contractError']['code'] == (
        'invalid_argument_length')


def test_direct_dispatch_rejects_missing_contract_in_v2_epoch():
    executable = _tool('guarded_read')
    task = {
        'id': 'task_missing_contract', 'convId': 'conv-missing-contract',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [executable],
        '_executable_tool_catalog': [executable],
        '_toolContractDocumentsByName': {},
    }
    assistant = {'content': '', 'tool_calls': [{
        'id': 'contract-direct-2', 'type': 'function',
        'function': {'name': 'guarded_read', 'arguments': '{}'},
    }]}

    parsed, _ = parse_tool_calls(
        assistant, task, round_num=0, tool_round_num=0,
        project_enabled=False)

    assert '[tool_contract_unavailable]' in parsed[0][6]
    assert parsed[0][5]['status'] == 'rejected'
    assert parsed[0][5]['_contractError']['retryable'] is True


def test_executable_catalog_is_the_direct_call_authority():
    executable = _tool('guessed_available_tool')
    task = {
        'id': 'task_executable_authority', 'convId': 'conv-executable',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [_tool('visible')],
        '_executable_tool_catalog': [executable],
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
        desktop_enabled=False,
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
    code = search_executable_catalog(
        catalog, 'find symbol references in code', limit=2)
    assert _names([{'function': {'name': row['name']}}
                   for row in code['items']])[0] == 'grep_search'

    memory = search_executable_catalog(
        catalog, '找回之前拍板的决定', limit=2)
    assert memory['items'][0]['name'] == 'memory_search'


def test_local_search_ranks_non_obvious_builtin_capabilities_from_user_language():
    from lib.tools.registry import ToolContext, assemble_tool_list

    cases = (
        ('帮我做个介绍量子计算的视频', 'produce_video'),
        ('给我弄个新品发布会 PPT', 'produce_slides'),
        ('生成一张发布会封面图', 'generate_image'),
        ('把刚写的前端页面实际渲染一下看看效果', 'browser_preview_page'),
    )
    for query, expected in cases:
        ctx = ToolContext(
            cfg={'memoryEnabled': True, 'mcpEnabled': False},
            task_id='capability-search', project_path='/project',
            project_enabled=True, search_mode='single', search_enabled=True,
            fetch_enabled=True, code_exec_enabled=True,
            browser_enabled=True, desktop_enabled=True,
            image_gen_enabled=True, human_guidance_enabled=False,
            scheduler_enabled=True,
            messages=[{'role': 'user', 'content': query}],
        )
        catalog, _ = assemble_tool_list(ctx)
        result = search_executable_catalog(
            catalog, query, limit=5,
            search_text_by_name=ctx.search_text_by_name)
        assert result['items'][0]['name'] == expected


def test_private_search_hints_improve_ranking_but_never_leak_to_results():
    catalog = [
        _tool('project_message', description='Message a project peer.'),
        _tool('mcp__chat__post', description='Post to a channel.'),
    ]
    secret_hint = 'notify coworkers team group chat private-index-marker'
    result = search_executable_catalog(
        catalog, 'tell everyone in the team chat', limit=2,
        search_text_by_name={'mcp__chat__post': secret_hint})

    assert result['items'][0]['name'] == 'mcp__chat__post'
    assert 'private-index-marker' not in str(result)


def test_builtin_specs_carry_per_function_private_search_hints():
    from lib.tools.registry import all_specs

    by_key = {spec.key: spec for spec in all_specs()}
    assert 'symbol references' in by_key['project'].search_hints['grep_search']
    assert 'previous decision' in by_key['memory'].search_hints[
        'search_memories']
    assert 'cancel stop' in by_key['scheduler'].search_hints[
        'schedule_manage']
    assert '科普视频' in by_key['produce'].search_hints['produce_video']
    assert 'pptx' in by_key['produce'].search_hints['produce_slides']
    assert '封面图' in by_key['image_gen'].search_hints['generate_image']
    assert '真实浏览器' in by_key['page_preview'].search_hints[
        'browser_preview_page']


def test_explicitly_empty_authority_never_falls_back_to_latched_wire_schema():
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    task = {
        'id': 'task-empty-authority', 'convId': 'conv-empty-authority',
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        'toolRounds': [], 'aborted': False,
        '_tool_schema': [_tool('read_files')],
        '_executable_tool_catalog': [],
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
        '_executable_tool_catalog': authority,
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
        '_executable_tool_catalog': authority,
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
        {**base, '_executable_tool_catalog': frozen}, api_protocol='openai',
        base_url='https://compatible.example/v1')
    after = prepare_request(
        {**base,
         '_executable_tool_catalog': frozen + [_tool('new_live_tool')]},
        api_protocol='openai', base_url='https://compatible.example/v1')

    assert before.body['tools'] == after.body['tools']
    assert _names(after.body['tools']) == [
        'tool_0', SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME]


def test_hidden_execute_gateway_is_admitted_when_model_guesses_it():
    """The wrapper costs no schema tokens but a correctly shaped call works."""
    from lib.tasks_pkg.tool_dispatch._labels import _known_tool_names

    task = {
        'id': 'task-no-execute-wrapper',
        '_executable_tool_catalog': [_tool('read_doc')],
        '_tool_gateway_names': [EXECUTE_TOOLS_NAME, SEARCH_TOOLS_NAME],
    }
    assert EXECUTE_TOOLS_NAME in _known_tool_names(task)
    # Schema-less recovery still preserves the registered gateway path.
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

    from lib.tools.registry import all_specs
    gateway_spec = next(spec for spec in all_specs()
                        if spec.key == 'tool_gateway')
    assert gateway_spec.provides == frozenset({
        SEARCH_TOOLS_NAME, EXECUTE_TOOLS_NAME})


def test_round_assembly_admits_hidden_execute_without_exposing_schema(
        monkeypatch):
    import lib.tasks_pkg.orchestrator._tool_assembly_prep as prep
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
        'image_gen_enabled': False,
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
        'model': 'test', '_executable_tool_catalog': [_tool(
            'scale', properties={'count': {'type': 'integer'}},
            required=['count'])],
        '_executableToolNamespaceByName': {},
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


def test_execute_gateway_receipts_handle_kimi_recycled_ids_and_stay_bounded(
        monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    executed = []
    finalized = []

    def fake_execute(task, calls, execution, **kwargs):
        count = calls[0]['_normalized_arguments']['count']
        executed.append(count)
        return [{
            'call_id': calls[0]['id'], 'name': 'scale', 'status': 'done',
            'approval': {'required': False, 'status': 'not_required'},
            'duration': 1, 'source': 'execute_calls', 'output': str(count),
        }]

    monkeypatch.setattr(handler, '_execute_normalized', fake_execute)
    monkeypatch.setattr(
        handler, '_finalize',
        lambda *_args, **kwargs: finalized.append(bool(kwargs.get('ok'))))
    task = {
        'model': 'kimi-k3', '_executable_tool_catalog': [_tool(
            'scale', properties={'count': {'type': 'integer'}},
            required=['count'])],
        '_executableToolNamespaceByName': {},
    }

    def invoke(count, llm_round, *, call_id='execute_tools_0'):
        return handler.handle_execute_tools(
            task, {}, EXECUTE_TOOLS_NAME, call_id,
            {'calls': [{'name': 'scale', 'arguments': {'count': count}}]},
            llm_round + 1, {'llmRound': llm_round}, {}, None, False)[1]

    first = invoke(1, 0)
    second = invoke(2, 1)
    replay = invoke(2, 1)
    assert json.loads(first)['results'][0]['output'] == '1'
    assert json.loads(second)['results'][0]['output'] == '2'
    assert replay == second
    assert executed == [1, 2], (
        'a recycled positional ID must execute new args, while an exact frame '
        'replay in the same round must not execute twice')

    for llm_round in range(2, 270):
        invoke(llm_round, llm_round)
    assert len(task['_execute_gateway_receipts']) <= 256
    assert all(finalized)


def test_execute_gateway_rejects_ambiguous_round_before_child_execution(
        monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    executed = []
    monkeypatch.setattr(
        handler, '_execute_normalized',
        lambda *_args, **_kwargs: executed.append(True))
    task = {
        'id': 'gateway-round-identity',
        '_executable_tool_catalog': [_tool('scale')],
        '_executableToolNamespaceByName': {},
    }

    with pytest.raises(ValueError, match='integer llmRound'):
        handler.handle_execute_tools(
            task, {}, EXECUTE_TOOLS_NAME, 'execute_tools_0',
            {'calls': [{'name': 'scale', 'arguments': {}}]},
            1, {'llmRound': ['corrupt']}, {}, None, False)

    assert executed == []
    assert task.get('_execute_gateway_receipts') == {}


def test_gateway_synthetic_round_allocator_ignores_malformed_rows():
    from lib.tasks_pkg.handlers.tool_gateway import _gateway_round_base

    task = {'toolRounds': [
        None,
        {'roundNum': '8700001'},
        {'roundNum': ['corrupt']},
        {'roundNum': 8_700_004},
    ]}

    assert _gateway_round_base(task) == 8_700_005


def test_execute_gateway_replay_preserves_error_verdict(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    verdicts = []
    monkeypatch.setattr(
        handler, '_finalize',
        lambda *_args, **kwargs: verdicts.append(bool(kwargs.get('ok'))))
    task = {
        'model': 'kimi-k3',
        '_executable_tool_catalog': [_tool('only_available_tool')],
        '_executableToolNamespaceByName': {},
    }
    args = {'calls': [{
        'name': 'missing_tool', 'arguments': {},
    }]}
    for _attempt in range(2):
        _tc_id, content, _aborted = handler.handle_execute_tools(
            task, {}, EXECUTE_TOOLS_NAME, 'execute_tools_0', args,
            1, {'llmRound': 0}, {}, None, False)
        assert json.loads(content)['status'] == 'error'

    assert verdicts == [False, False], (
        'replaying a cached gateway failure must never project it as done')


def test_execute_handler_rejects_nested_call_before_dispatch(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler
    from lib.tools.contracts import adapt_legacy_tool_contract

    catalog_tool = _tool(
        'read_batch', properties={
            'ids': {'type': 'array', 'items': {'type': 'string'}},
        }, required=['ids'])
    contract_tool = _tool(
        'read_batch', properties={
            'ids': {'type': 'array', 'minItems': 1,
                    'items': {'type': 'string'}},
        }, required=['ids'])
    document = adapt_legacy_tool_contract(contract_tool).search_document()

    def must_not_execute(*_args, **_kwargs):
        raise AssertionError('contract-rejected child reached execution')

    monkeypatch.setattr(handler, '_execute_normalized', must_not_execute)
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'model': 'test', '_executable_tool_catalog': [catalog_tool],
        '_executableToolNamespaceByName': {},
        '_toolContractDocumentsByName': {'read_batch': document},
    }

    _, content, aborted = handler.handle_execute_tools(
        task, {}, EXECUTE_TOOLS_NAME, 'contract-wrapper-1',
        {'calls': {'tool': 'read_batch', 'args': {'ids': []}}},
        1, {}, {}, None, False)

    payload = json.loads(content)
    assert aborted is False
    assert payload['status'] == 'error'
    assert payload['errors'][0]['code'] == 'too_few_items'


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
        'model': 'test', '_executable_tool_catalog': [_tool(
            'scale', description='Scale a number.',
            properties={'count': {'type': 'integer'}}, required=['count'])],
        '_executableToolNamespaceByName': {},
        '_executableToolSearchTextByName': {},
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


def test_execute_program_does_not_truthiness_repair_invalid_arguments(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    seen = []

    def must_not_execute(task, calls, execution, **kwargs):
        seen.extend(calls)
        raise AssertionError('invalid program arguments reached execution')

    monkeypatch.setattr(handler, '_execute_normalized', must_not_execute)
    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'model': 'test', '_executable_tool_catalog': [_tool(
            'scale', description='Scale a number.',
            properties={'count': {'type': 'integer'}}, required=['count'])],
        '_executableToolNamespaceByName': {},
        '_executableToolSearchTextByName': {},
    }

    _, content, aborted = handler.handle_execute_tools(
        task, {}, EXECUTE_TOOLS_NAME, 'program-wrapper-invalid-args', {
            'program': 'return tools.call("scale",[]);'},
        1, {'llmRound': 0}, {}, None, False)

    payload = json.loads(content)
    assert aborted is False
    assert seen == []
    assert payload['program']['result']['status'] == 'error'
    assert 'arguments' in json.dumps(
        payload['program']['result']['error']).lower()


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
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

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
        '_userId': 1,
        'status': 'running', 'aborted': False, 'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': True,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, 'toolRounds': [],
        '_executable_tool_catalog': catalog,
    }
    result = _execute_call_batch(
        task, normalized['calls'], cfg={'autoApply': False},
        project_path=None, project_enabled=False, model='test', llm_round=0)

    assert approvals == [('write_file', {'path': 'x.txt',
                                         'content': 'danger'})]
    assert result[0]['status'] == 'rejected'
    assert result[0]['error'] == 'User rejected this write.'


def test_execute_gateway_nested_snapshot_keeps_tool_call_result_pair(
        monkeypatch):
    """Nested gateway execution must never build a result-only transcript.

    Attended tasks emit a post-tool wire snapshot inside the shared pipeline.
    The gateway used to pass an empty local message list, so the pipeline
    appended only ``role=tool`` and the sanitizer necessarily removed it as
    an orphan.  Capture the snapshot boundary and require the assistant
    carrier and result to stay paired.
    """
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    from lib.tasks_pkg import wire_messages

    captured = []
    real_sanitize = wire_messages.apply_wire_sanitize

    def capture_sanitize(messages, **kwargs):
        captured.append([dict(message) for message in messages])
        return real_sanitize(messages, **kwargs)

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        return tc_id, 'nested-result', False

    monkeypatch.setattr(wire_messages, 'apply_wire_sanitize', capture_sanitize)
    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)

    catalog = [_tool('nested_read')]
    task = {
        'id': 'gateway-snapshot-pair', 'convId': 'gateway-snapshot-conv',
        '_userId': 1,
        'status': 'running', 'aborted': False, 'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': True,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, 'toolRounds': [],
        '_executable_tool_catalog': catalog,
    }
    call = {
        'id': 'nested-call-id', 'type': 'function',
        'source': 'execute_calls', '_normalized_arguments': {},
        'function': {'name': 'nested_read', 'arguments': '{}'},
    }

    result = _execute_call_batch(
        task, [call], cfg={'autoApply': True}, project_path=None,
        project_enabled=False, model='test', llm_round=0)

    assert result[0]['output'] == 'nested-result'
    assert captured
    local_transcript = captured[-1]
    assert local_transcript[0]['role'] == 'assistant'
    call_ids = {
        tool_call['id']
        for message in local_transcript
        for tool_call in message.get('tool_calls') or []
    }
    result_ids = {
        message['tool_call_id'] for message in local_transcript
        if message.get('role') == 'tool'
    }
    assert call_ids == result_ids == {call['id']}


def test_local_program_reduces_pre_compaction_result_on_bounded_private_lane(
        monkeypatch):
    """ToolScript sees raw evidence while model/UI receipts remain L0-bound."""
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    import lib.tasks_pkg.compaction._budget as _budget
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline
    from lib.tools.programmatic import ProgrammaticResultBudget

    raw = (
        'RAW_INTERNAL_PREFIX\n'
        + ('large source evidence line\n' * 20_000)
        + 'RAW_INTERNAL_TAIL'
    )

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        return tc_id, raw, False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(
        _budget, '_store_tool_result_artifact',
        lambda *_args, **_kwargs: 'tool-result:' + 'r' * 64)
    catalog = [_tool('large_read')]
    task = {
        'id': 'program-private-lane', 'convId': 'program-private-conv',
        '_userId': 1, 'status': 'running', 'aborted': False, 'model': 'test',
        'events': [], 'events_lock': threading.Lock(), '_attended': False,
        'config': {'tools': {'resultEnvelope': 'v2'}}, 'toolRounds': [],
        '_executable_tool_catalog': catalog,
    }
    call = {
        'id': 'program-child', 'type': 'function',
        'source': 'execute_program', '_normalized_arguments': {},
        'function': {'name': 'large_read', 'arguments': '{}'},
    }
    budget = ProgrammaticResultBudget()

    receipts = _execute_call_batch(
        task, [call], cfg={'autoApply': True}, project_path=None,
        project_enabled=False, model='test', llm_round=0,
        program_result_budget=budget)

    assert receipts[0]['output'] == raw
    assert receipts[0]['output_truncated'] is False
    assert receipts[0]['raw_output_bytes'] == len(raw.encode('utf-8'))
    assert budget.stats()['outputBytes'] == len(raw.encode('utf-8'))
    child_row = next(row for row in task['toolRounds']
                     if row.get('toolCallId') == call['id'])
    model_visible = child_row['toolContent']
    assert model_visible != raw
    assert 'RAW_INTERNAL_PREFIX' in model_visible
    assert 'RAW_INTERNAL_TAIL' not in model_visible
    assert json.loads(model_visible)['artifactRef'].startswith('tool-result:')


def test_provider_boundary_injects_execute_only_for_local_search():
    catalog = [_tool(f'tool_{index}') for index in range(12)]
    base = {
        'model': 'qwen-test',
        'messages': [{'role': 'user', 'content': 'find a tool'}],
        'tools': catalog,
        '_executable_tool_catalog': catalog,
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


def test_toolscript_documents_and_executes_common_bounded_reductions():
    value, stats = execute_toolscript(
        "let parsed=JSON.parse('{\"rows\":[\" alpha \",\"beta\",\"gamma\"]}'); "
        "let rows=parsed.rows.slice(0,2).map(x => x.trim()); "
        "let joined=rows.join('|'); "
        "let parts=joined.split('|'); "
        "let keys=Object.keys(parsed); "
        "return {json:JSON.stringify({first:parts[0]}), keys:keys, "
        "hasBeta:rows.includes('beta')};",
        search=lambda *args: [], call=lambda *args: None,
        call_many=lambda *args: [])

    assert value == {
        'json': '{"first":"alpha"}',
        'keys': ['rows'],
        'hasBeta': True,
    }
    assert stats['steps'] < 100_000

    with pytest.raises(ToolScriptError) as unsupported:
        execute_toolscript(
            "return 'x'.repeat(2);", search=lambda *args: [],
            call=lambda *args: None, call_many=lambda *args: [])
    assert unsupported.value.code == 'unsafe_call'


def test_toolscript_repairs_only_unambiguous_missing_object_commas():
    source = (
        "let result={count:2}; "
        "return {status:'ok' result:result, trailing:true,};"
    )
    value, stats = execute_toolscript(
        source, search=lambda *args: [],
        call=lambda *args: None, call_many=lambda *args: [])

    assert value == {'status': 'ok', 'result': {'count': 2}, 'trailing': True}
    assert stats['syntax_repairs'] == [{
        'kind': 'missing_object_comma',
        'offset': source.index('result:result'),
    }]

    # A bare adjacent identifier is ambiguous and must keep failing instead of
    # being reinterpreted as an object member.
    with pytest.raises(ToolScriptError) as ambiguous:
        execute_toolscript(
            "return {status:'ok' result};", search=lambda *args: [],
            call=lambda *args: None, call_many=lambda *args: [])
    assert ambiguous.value.code == 'syntax_error'

    # Syntax recovery must not bypass the forbidden-member boundary.
    with pytest.raises(ToolScriptError) as unsafe:
        execute_toolscript(
            "return {status:'ok' constructor:1};", search=lambda *args: [],
            call=lambda *args: None, call_many=lambda *args: [])
    assert unsafe.value.code == 'unsafe_member'

    over_limit = 'return {' + ' '.join(
        f'key{index}:{index}' for index in range(10)) + '};'
    with pytest.raises(ToolScriptError) as excessive:
        execute_toolscript(
            over_limit, search=lambda *args: [], call=lambda *args: None,
            call_many=lambda *args: [])
    assert excessive.value.code == 'syntax_error'
    assert excessive.value.detail['limit'] == 8
    assert len(excessive.value.detail['syntax_repairs']) == 8


def test_shared_pipeline_executes_exact_reemit_and_recycled_id(
        monkeypatch):
    """A completed call id is NEVER receipt-replayed: an exact same-id+
    same-args re-emit EXECUTES again (the model deliberately re-issued it —
    re-read after an edit, re-run after a fix; replaying the stale receipt
    made edits report success without running and re-reads return pre-edit
    bytes — tasks f8149620/0c2e3a92, 2026-08-19), and the same id recycled
    with DIFFERENT args is likewise a fresh call — positional-id models
    (kimi-k3 ``{tool}_{index-in-message}``) cannot mint fresh ids, so
    rejecting a recycled id locked the tool out for the rest of the task and
    pushed the model into sacrificial-call superstitions (conv mswu06rpir1hwv,
    the ``search_tools query="noop ping placeholder"`` burn, 2026-08-17)."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append((fn_name, dict(fn_args)))
        return tc_id, f'ran:{fn_args.get("value")}', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)

    task = {
        'id': 'call-id-task', 'convId': 'call-id-conv', 'status': 'running',
        '_userId': 1,
        'aborted': False, 'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
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
    reemit_messages, reemit_row = run(parsed(1, 2))
    conflict_messages, conflict_row = run(parsed(2, 3))
    recycle_reemit_messages, recycle_reemit_row = run(parsed(2, 4))

    # Every emission EXECUTED — no receipt replay, no stale content. The exact
    # re-emit produces the same text only because this fake is deterministic;
    # the behavioural pin is the second execution itself.
    assert executions == [
        ('side_effect_tool', {'value': 1}),
        ('side_effect_tool', {'value': 1}),
        ('side_effect_tool', {'value': 2}),
        ('side_effect_tool', {'value': 2}),
    ]
    assert first_messages[-1]['content'] == 'ran:1'
    assert reemit_messages[-1]['content'] == 'ran:1'
    assert not reemit_row.get('_idempotentReplay')
    # Recycled id with new args: executes as a fresh call.
    assert conflict_messages[-1]['content'] == 'ran:2'
    # The fake executor skips _finalize_tool_round, so the row keeps its
    # dispatch-time status here; the behavioral pin is that the conflict was
    # EXECUTED (not rejected): no replay marker, fresh content above.
    assert conflict_row['status'] != 'rejected'
    assert not conflict_row.get('_idempotentReplay')
    assert recycle_reemit_messages[-1]['content'] == 'ran:2'
    assert not recycle_reemit_row.get('_idempotentReplay')
    # Every re-issued id was reminted — the wire never carries two
    # tool_call/tool_result pairs with the same id across rounds.
    assert first_row['toolCallId'] == 'stable-call-id'
    assert reemit_row['toolCallId'] != 'stable-call-id'
    assert conflict_row['toolCallId'] != 'stable-call-id'
    assert recycle_reemit_row['toolCallId'] != 'stable-call-id'


def _gateway_child(call_id, name, arguments):
    return {
        'id': call_id,
        'type': 'function',
        'source': 'execute_calls',
        '_normalized_arguments': dict(arguments),
        'function': {
            'name': name,
            'arguments': json.dumps(arguments, separators=(',', ':')),
        },
    }


def test_execute_gateway_delegates_identical_direct_child_one_to_one(
        monkeypatch):
    """A direct sibling owns one identical calls[] occurrence, not both."""
    import lib.tasks_pkg.handlers.tool_gateway as gateway_handler
    from lib.tasks_pkg.tool_dispatch._flags import (
        _publish_execute_gateway_direct_siblings,
    )

    task = {
        'id': 'cross-channel-task',
        'model': 'test-model',
    }
    _publish_execute_gateway_direct_siblings(task, 4, [
        ('mcp__llm__experiment_run', 'direct-1',
         {'dry_run': True, 'ids': '355208'}),
    ])
    calls = [
        _gateway_child(
            'child-1', 'mcp__llm__experiment_run',
            {'dry_run': True, 'ids': '355208'}),
        _gateway_child(
            'child-2', 'mcp__llm__experiment_run',
            {'dry_run': True, 'ids': '355208'}),
    ]
    executed_batches = []
    audits = []

    def fake_execute(_task, batch, _execution, **_kwargs):
        executed_batches.append(list(batch))
        return [{
            'call_id': call['id'],
            'name': call['function']['name'],
            'status': 'done',
            'output': 'executed child',
        } for call in batch]

    monkeypatch.setattr(gateway_handler, '_execute_normalized', fake_execute)
    monkeypatch.setattr(
        gateway_handler, 'audit_log',
        lambda event, **details: audits.append((event, details)))

    results = gateway_handler._execute_calls_with_direct_delegation(
        task, calls, 'auto', cfg={}, project_path=None,
        project_enabled=False, model='test-model', llm_round=4)

    assert executed_batches == [[calls[1]]]
    assert [result['status'] for result in results] == ['delegated', 'done']
    assert results[0]['delegation'] == {
        'kind': 'same_response_direct_call',
        'direct_call_id': 'direct-1',
    }
    assert 'Not executed here' in results[0]['output']
    assert audits == [(
        'execute_gateway_child_delegated',
        {
            'task_id': 'cross-channel-task',
            'model': 'test-model',
            'tool': 'mcp__llm__experiment_run',
            'direct_call_id': 'direct-1',
            'gateway_child_call_id': 'child-1',
            'llm_round': 4,
        },
    )]


def test_execute_gateway_delegation_preserves_order_and_argument_differences(
        monkeypatch):
    """FIFO pairing keeps direct cardinality and never absorbs different args."""
    import lib.tasks_pkg.handlers.tool_gateway as gateway_handler
    from lib.tasks_pkg.tool_dispatch._flags import (
        _publish_execute_gateway_direct_siblings,
    )

    task = {'id': 'pairing-task', 'model': 'test'}
    _publish_execute_gateway_direct_siblings(task, 2, [
        ('read_doc', 'direct-a', {'id': 'same'}),
        ('read_doc', 'direct-b', {'id': 'same'}),
    ])
    calls = [
        _gateway_child('child-a', 'read_doc', {'id': 'same'}),
        _gateway_child('child-different', 'read_doc', {'id': 'different'}),
        _gateway_child('child-b', 'read_doc', {'id': 'same'}),
    ]
    executed_batches = []

    def fake_execute(_task, batch, _execution, **_kwargs):
        executed_batches.append(list(batch))
        return [{
            'call_id': call['id'], 'name': call['function']['name'],
            'status': 'done', 'output': call['id'],
        } for call in batch]

    monkeypatch.setattr(gateway_handler, '_execute_normalized', fake_execute)
    monkeypatch.setattr(gateway_handler, 'audit_log', lambda *args, **kwargs: None)

    results = gateway_handler._execute_calls_with_direct_delegation(
        task, calls, 'parallel', cfg={}, project_path=None,
        project_enabled=False, model='test', llm_round=2)

    assert executed_batches == [[calls[1]]]
    assert [result['status'] for result in results] == [
        'delegated', 'done', 'delegated']
    assert [results[0]['delegation']['direct_call_id'],
            results[2]['delegation']['direct_call_id']] == [
                'direct-a', 'direct-b']
    assert results[1]['call_id'] == 'child-different'


def test_execute_gateway_scope_and_same_channel_calls_do_not_delegate(
        monkeypatch):
    """Only the same top-level response can donate direct occurrences."""
    import lib.tasks_pkg.handlers.tool_gateway as gateway_handler
    from lib.tasks_pkg.tool_dispatch._flags import (
        _publish_execute_gateway_direct_siblings,
    )

    call = _gateway_child('child-1', 'read_doc', {'id': 'same'})
    task = {'id': 'scope-task', 'model': 'test'}
    _publish_execute_gateway_direct_siblings(
        task, 7, [('read_doc', 'direct-1', {'id': 'same'})])
    executed_batches = []

    def fake_execute(_task, batch, _execution, **_kwargs):
        executed_batches.append(list(batch))
        return [{
            'call_id': child['id'], 'name': child['function']['name'],
            'status': 'done', 'output': child['id'],
        } for child in batch]

    monkeypatch.setattr(gateway_handler, '_execute_normalized', fake_execute)

    stale_round = gateway_handler._execute_calls_with_direct_delegation(
        task, [call], 'auto', cfg={}, project_path=None,
        project_enabled=False, model='test', llm_round=8)
    assert stale_round[0]['status'] == 'done'

    task_without_direct = {'id': 'same-channel-task', 'model': 'test'}
    same_channel = gateway_handler._execute_calls_with_direct_delegation(
        task_without_direct, [call, dict(call, id='child-2')], 'auto',
        cfg={}, project_path=None, project_enabled=False,
        model='test', llm_round=7)
    assert [result['status'] for result in same_channel] == ['done', 'done']
    assert len(executed_batches) == 2
    assert len(executed_batches[1]) == 2


def test_shared_pipeline_executes_direct_and_delegates_identical_gateway_child(
        monkeypatch):
    """End to end: the screenshot shape reaches the leaf exactly once."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline

    leaf_name = 'mcp__llm__experiment_run'
    leaf_args = {'dry_run': True, 'ids': '355208'}
    wrapper_args = {
        'calls': [{
            'name': leaf_name,
            'arguments': leaf_args,
        }],
        'execution': 'auto',
    }
    task = {
        'id': 'screenshot-shape-task', 'convId': 'screenshot-shape-conv',
        '_userId': 1, 'status': 'running', 'aborted': False,
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        '_attended': False, '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0, 'toolRounds': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        '_executable_tool_catalog': [
            _tool(leaf_name, properties={
                'dry_run': {'type': 'boolean'},
                'ids': {'type': 'string'},
            }, required=['dry_run', 'ids']),
        ],
    }
    parsed = []
    for sequence, (name, call_id, arguments) in enumerate([
        (EXECUTE_TOOLS_NAME, 'execute_tools_129', wrapper_args),
        (leaf_name, 'mcp__llm__experiment_run_130', leaf_args),
    ], 1):
        _, row, _ = _build_tool_round_entry(
            name, arguments, call_id, json.dumps(arguments), sequence, False)
        row['llmRound'] = 5
        task['toolRounds'].append(row)
        parsed.append(({
            'id': call_id,
            'type': 'function',
            'function': {
                'name': name,
                'arguments': json.dumps(arguments),
            },
        }, name, call_id, dict(arguments), row['roundNum'], row, None))

    leaf_executions = []
    real_execute = pipeline._execute_tool_one

    def observe_execute(task_arg, tc, fn_name, tc_id, fn_args, rn,
                        round_entry, cfg, project_path, project_enabled,
                        all_tools=None):
        if fn_name == leaf_name:
            leaf_executions.append((tc_id, dict(fn_args)))
            return tc_id, 'dry-run token', False
        return real_execute(
            task_arg, tc, fn_name, tc_id, fn_args, rn, round_entry,
            cfg, project_path, project_enabled, all_tools=all_tools)

    monkeypatch.setattr(pipeline, '_execute_tool_one', observe_execute)
    monkeypatch.setattr(heartbeat, '_execute_tool_one', observe_execute)
    messages = []

    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None,
        project_enabled=False,
        tool_list=task['_executable_tool_catalog'], messages=messages,
        all_search_results_text=[], round_num=5, model='test')

    assert leaf_executions == [(
        'mcp__llm__experiment_run_130', leaf_args)]
    gateway_result = next(
        message for message in messages
        if message.get('tool_call_id') == 'execute_tools_129')
    payload = json.loads(gateway_result['content'])
    assert payload['status'] == 'ok'
    assert payload['results'][0]['status'] == 'delegated'
    assert payload['results'][0]['delegation']['direct_call_id'] \
        == 'mcp__llm__experiment_run_130'
    direct_result = next(
        message for message in messages
        if message.get('tool_call_id') == 'mcp__llm__experiment_run_130')
    assert direct_result['content'] == 'dry-run token'
    wrapper_round = next(
        row for row in task['toolRounds']
        if row.get('toolCallId') == 'execute_tools_129')
    assert wrapper_round['_delegatedChildren'] == 1


def test_nested_gateway_pipeline_cannot_replace_top_level_direct_registry(
        monkeypatch):
    """The synthetic child assistant is not a new provider response owner."""
    import lib.tasks_pkg.handlers.tool_gateway as gateway_handler
    import lib.tasks_pkg.tool_dispatch._pipeline as pipeline
    from lib.tasks_pkg.tool_dispatch._flags import (
        _publish_execute_gateway_direct_siblings,
    )

    task = {
        'id': 'nested-registry-task', 'convId': 'nested-registry-conv',
        '_userId': 1, 'status': 'running', 'aborted': False,
        'model': 'test', 'events': [], 'events_lock': threading.Lock(),
        '_attended': False, '_dispatch_heartbeat': 0.0,
        '_t_last_event': 0.0, 'toolRounds': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        '_executable_tool_catalog': [_tool(
            'read_doc', properties={'id': {'type': 'string'}},
            required=['id'])],
    }
    _publish_execute_gateway_direct_siblings(
        task, 3, [('read_doc', 'direct-1', {'id': 'same'})])
    original_state = json.loads(json.dumps(
        task['_execute_gateway_direct_siblings']))
    observed_publish_flags = []
    real_pipeline = pipeline.execute_tool_pipeline

    def spy_pipeline(*args, **kwargs):
        observed_publish_flags.append(
            kwargs.get('publish_direct_gateway_siblings'))
        return real_pipeline(*args, **kwargs)

    def fake_execute(_task, _tc, _name, call_id, _args, _rn, _row,
                     _cfg, _path, _enabled, all_tools=None):
        return call_id, 'nested executed', False

    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch.api.execute_tool_pipeline', spy_pipeline)
    monkeypatch.setattr(pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(
        'lib.tasks_pkg.tool_dispatch._heartbeat._execute_tool_one',
        fake_execute)

    receipts = gateway_handler._execute_call_batch(
        task, [_gateway_child('child-1', 'read_doc', {'id': 'different'})],
        cfg={'autoApply': True}, project_path=None, project_enabled=False,
        model='test', llm_round=3)

    assert receipts[0]['output'] == 'nested executed'
    assert observed_publish_flags == [False]
    assert task['_execute_gateway_direct_siblings'] == original_state


def test_gateway_parallel_batch_preserves_occurrences_past_worker_limit(
        monkeypatch):
    """One request stays one batch while the pipeline bounds worker fan-out."""
    import lib.tasks_pkg.handlers.tool_gateway as gateway_handler

    observed_batches = []

    def fake_execute_batch(_task, batch, **_kwargs):
        observed_batches.append(list(batch))
        return [{'call_id': call['id'], 'status': 'done'} for call in batch]

    monkeypatch.setattr(
        gateway_handler, '_execute_call_batch', fake_execute_batch)
    calls = [{
        'id': f'call-{index}',
        'source': 'execute_calls',
        'function': {'name': 'read_files', 'arguments': '{"path":"a"}'},
    } for index in range(9)]

    receipts = gateway_handler._execute_normalized(
        {}, calls, 'parallel', cfg={}, project_path=None,
        project_enabled=False, model='test', llm_round=0)

    assert len(observed_batches) == 1
    assert observed_batches[0] == calls
    assert len(receipts) == len(calls)


def test_gateway_sequential_calls_remain_fresh_occurrences(monkeypatch):
    """Sequential mode may intentionally observe mutations between calls."""
    import lib.tasks_pkg.handlers.tool_gateway as gateway_handler

    observed_batches = []

    def fake_execute_batch(_task, batch, **_kwargs):
        observed_batches.append(list(batch))
        return [{'call_id': call['id'], 'status': 'done'} for call in batch]

    monkeypatch.setattr(
        gateway_handler, '_execute_call_batch', fake_execute_batch)
    calls = [{
        'id': f'call-{index}',
        'source': 'execute_calls',
        'function': {'name': 'read_files', 'arguments': '{"path":"a"}'},
    } for index in range(2)]

    gateway_handler._execute_normalized(
        {}, calls, 'sequential', cfg={}, project_path=None,
        project_enabled=False, model='test', llm_round=0)

    assert observed_batches == [[calls[0]], [calls[1]]]


@pytest.mark.parametrize('provider_call_ids, values', [
    (('provider-call-a', 'provider-call-b'), (7, 7)),
    (('provider-call-reused', 'provider-call-reused'), (7, 7)),
    (('provider-call-conflict', 'provider-call-conflict'), (7, 8)),
])
def test_shared_pipeline_executes_response_occurrences_independently(
        monkeypatch, provider_call_ids, values):
    """Neither equal content nor a recycled ID collapses response positions."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append((fn_name, dict(fn_args)))
        return tc_id, f'executed:{fn_args["value"]}', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    # Correlation repair is unconditional, including legacy call-id mode.
    monkeypatch.setenv('TOFU_CANONICAL_CALL_IDS', '0')

    task = {
        'id': 'duplicate-batch-task', 'convId': 'duplicate-batch-conv',
        'status': 'running', '_userId': 1, 'aborted': False,
        'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }

    parsed = []
    for sequence, (call_id, value) in enumerate(
            zip(provider_call_ids, values), 1):
        _, row, _ = _build_tool_round_entry(
            'side_effect_tool', {'value': value}, call_id,
            json.dumps({'value': value}), sequence, False)
        task['toolRounds'].append(row)
        tc = {
            'id': call_id, 'type': 'function',
            'function': {
                'name': 'side_effect_tool', 'arguments': '{"value":7}',
            },
        }
        tc['function']['arguments'] = json.dumps({'value': value})
        parsed.append((tc, 'side_effect_tool', call_id, {'value': value},
                       row['roundNum'], row, None))

    messages = []
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test')

    assert len(executions) == 2
    assert {name for name, _arguments in executions} == {'side_effect_tool'}
    assert sorted(arguments['value'] for _name, arguments in executions) \
        == sorted(values)
    tool_messages = [message for message in messages
                     if message.get('role') == 'tool']
    assert len(tool_messages) == 2  # provider protocol still gets one receipt/id
    assert tool_messages[0]['tool_call_id'] != tool_messages[1]['tool_call_id']
    assert sorted(message['content'] for message in tool_messages) == sorted([
        f'executed:{values[0]}', f'executed:{values[1]}',
    ])
    assert all(not row.get('_duplicateSuppressed')
               for row in task['toolRounds'])


def test_shared_pipeline_keeps_rejected_diagnostics_independent(monkeypatch):
    """Each malformed call occurrence retains its own typed diagnostic."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    from lib.tool_rejection import stamp_tool_rejection
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    executions = []

    def must_not_execute(*args, **kwargs):
        executions.append((args, kwargs))
        raise AssertionError('rejected tool call reached execution')

    monkeypatch.setattr(_pipeline, '_execute_tool_one', must_not_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', must_not_execute)
    task = {
        'id': 'rejected-twins-task', 'convId': 'rejected-twins-conv',
        'status': 'running', '_userId': 1, 'aborted': False,
        'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }
    errors = ['invalid JSON at byte 3', 'missing required field `path`']
    parsed = []
    for sequence, error in enumerate(errors, 1):
        call_id = f'rejected-{sequence}'
        _, row, _ = _build_tool_round_entry(
            'side_effect_tool', {}, call_id, '{}', sequence, False)
        row['status'] = 'rejected'
        stamp_tool_rejection(
            row,
            {'kind': 'invalid_tool_arguments',
             'tool': 'side_effect_tool'},
            reason=error,
            retryable=True,
        )
        task['toolRounds'].append(row)
        tc = {
            'id': call_id, 'type': 'function',
            'function': {'name': 'side_effect_tool', 'arguments': '{}'},
        }
        parsed.append((tc, 'side_effect_tool', call_id, {},
                       row['roundNum'], row, error))

    messages = []
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test')

    assert executions == []
    tool_messages = [message for message in messages
                     if message.get('role') == 'tool']
    assert [message['content'] for message in tool_messages] == errors
    assert all(not row.get('_duplicateSuppressed')
               for row in task['toolRounds'])


def test_equal_calls_never_cross_caller_authority(monkeypatch):
    """A rejected subagent call cannot affect the authorized root occurrence."""
    from lib.tasks_pkg.tool_dispatch.api import execute_tool_pipeline
    from lib.tasks_pkg.tool_display import _build_tool_round_entry
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append((fn_name, dict(fn_args)))
        return tc_id, 'root executed', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    task = {
        'id': 'caller-authority-task', 'convId': 'caller-authority-conv',
        'status': 'running', '_userId': 1, 'aborted': False,
        'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0,
        'toolRounds': [],
    }
    parsed = []
    args = {'command': 'echo safe'}
    callers = [
        {'type': 'multi_agent', 'agent_name': '/worker'},
        None,
    ]
    for sequence, caller in enumerate(callers, 1):
        call_id = f'authority-{sequence}'
        _, row, _ = _build_tool_round_entry(
            'run_command', args, call_id, json.dumps(args), sequence, False)
        tc = {
            'id': call_id, 'type': 'function',
            'function': {
                'name': 'run_command', 'arguments': json.dumps(args),
            },
        }
        if caller is not None:
            tc['caller'] = caller
            row['caller'] = caller
        task['toolRounds'].append(row)
        parsed.append((tc, 'run_command', call_id, dict(args),
                       row['roundNum'], row, None))

    messages = []
    execute_tool_pipeline(
        task, parsed, cfg={'autoApply': True}, project_path=None,
        project_enabled=False, tool_list=[], messages=messages,
        all_search_results_text=[], round_num=0, model='test')

    assert executions == [('run_command', args)]
    tool_messages = [message for message in messages
                     if message.get('role') == 'tool']
    assert 'subagents are read-only' in tool_messages[0]['content']
    assert tool_messages[1]['content'] == 'root executed'
    assert not task['toolRounds'][1].get('_duplicateSuppressed')


def test_execute_gateway_children_never_replay_completed_call_ids(
        monkeypatch):
    """Gateway children ride the shared pipeline id-reuse detection: an exact
    same-id re-emit EXECUTES again (a program that re-runs a command must see
    its real fresh output — never the stale receipt), and a recycled id with
    new args must EXECUTE FRESH — never silently serve the old result for a
    different call."""
    from lib.tasks_pkg.handlers.tool_gateway import _execute_call_batch
    import lib.tasks_pkg.tool_dispatch._heartbeat as _heartbeat
    import lib.tasks_pkg.tool_dispatch._pipeline as _pipeline

    executions = []

    def fake_execute(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                     cfg, project_path, project_enabled, all_tools=None):
        executions.append(dict(fn_args))
        return tc_id, f'ran:{fn_args["value"]}', False

    monkeypatch.setattr(_pipeline, '_execute_tool_one', fake_execute)
    monkeypatch.setattr(_heartbeat, '_execute_tool_one', fake_execute)
    task = {
        'id': 'gateway-child-id', 'convId': 'gateway-child-conv',
        '_userId': 1,
        'status': 'running', 'aborted': False, 'model': 'test', 'events': [],
        'config': {'tools': {'resultEnvelope': 'legacy'}},
        'events_lock': threading.Lock(), '_attended': False,
        '_dispatch_heartbeat': 0.0, '_t_last_event': 0.0, 'toolRounds': [],
        '_executable_tool_catalog': [_tool(
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
    reemit = _execute_call_batch(task, [child(1)], **common)
    recycled = _execute_call_batch(task, [child(2)], **common)

    assert executions == [{'value': 1}, {'value': 1}, {'value': 2}]
    assert first[0]['output'] == 'ran:1'
    # Exact re-emit: executed again (fresh result), no receipt replay.
    assert reemit[0]['output'] == 'ran:1'
    assert reemit[0]['status'] == 'done'
    # Recycled id, different args: a real fresh result, not the stale one.
    assert recycled[0]['status'] == 'done'
    assert recycled[0]['output'] == 'ran:2'

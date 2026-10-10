"""Exercise direct-call protocols at the final, shared provider boundary."""
from copy import deepcopy

import pytest

from lib.llm._sse_core import prepare_request
from lib.model_info._openai_gpt56 import GPT56_ALIAS_TARGET
from lib.tools.direct_wire import preserve_direct_wire_tools

pytestmark = pytest.mark.unit


def _tool(name, *, description='Read-only optional inspection.'):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': {
            'delivery': {'type': 'string', 'minLength': 1, 'maxLength': 24000}},
            'required': ['delivery'], 'additionalProperties': False}}}


def _functions(tools):
    result = {}
    for tool in tools or ():
        if tool.get('type') == 'namespace':
            result.update(_functions(tool.get('tools')))
        elif isinstance(tool.get('function'), dict):
            result[tool['function']['name']] = tool['function']
        elif tool.get('type') == 'function':
            result[tool['name']] = tool
        elif 'input_schema' in tool:
            result[tool['name']] = {**tool, 'parameters': tool['input_schema']}
    return result


def _body(*, available=True, stale=False):
    critical = _tool('seal_result', description='Sole direct final delivery.')
    optional = [_tool('lookup_' + str(i)) for i in range(25)]
    authority = ([critical] if available else []) + optional
    return {
        'model': 'gemini-3.7-flash',
        'messages': [{'role': 'user', 'content': 'Finish the work.'}],
        'tools': [critical] + optional,
        '_tool_wire_catalog': optional if stale else [critical] + optional,
        '_executable_tool_catalog': authority,
        '_required_direct_tool_names': ['seal_result'],
        '_tool_discovery_policy_by_name': {
            tool['function']['name']: 'searchable' for tool in authority},
        '_tool_search_mode': 'local', '_tool_schema_budget_tokens': 1,
    }


@pytest.mark.parametrize('search_mode', ['local', 'off'])
@pytest.mark.parametrize('exposure', ['additive', 'gateway_only'])
@pytest.mark.parametrize('stale', [False, True])
def test_required_schema_survives_budget_and_ptc(monkeypatch, search_mode, exposure, stale):
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints', lambda *a, **kw: None)
    body = _body(stale=stale)
    body.update(_tool_search_mode=search_mode, _programmatic_tool_calling='auto',
                _programmatic_eligible_tools=['lookup_0'],
                _programmatic_exposure=exposure, _programmatic_tier='program')
    before = deepcopy(body)
    plan = prepare_request(body, base_url='https://example.test/v1', api_key='test-key')
    function = _functions(plan.body.get('tools'))['seal_result']
    assert function['parameters'] == before['_executable_tool_catalog'][0]['function']['parameters']
    assert function['description'] == 'Sole direct final delivery.'
    assert body == before
    assert '_required_direct_tool_names' not in plan.body
    assert len(before['_executable_tool_catalog']) == 26


@pytest.mark.parametrize('protocol,model,url', [
    ('responses', GPT56_ALIAS_TARGET, 'https://api.openai.com/v1'),
    ('anthropic', 'claude-opus-4.6', 'https://api.anthropic.com'),
])
def test_native_search_never_defers_declared_direct_schema(monkeypatch, protocol, model, url):
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints', lambda *a, **kw: None)
    body = _body(stale=True)
    body.update(model=model, _tool_search_mode='native', _responses_feature_profile='openai')
    before = deepcopy(body)
    plan = prepare_request(body, base_url=url, api_key='test-key', api_protocol=protocol)
    assert plan.tool_search_backend == ('native_openai' if protocol == 'responses' else 'native_anthropic')
    assert any(tool.get('type', '').startswith('tool_search') for tool in plan.body['tools'])
    function = _functions(plan.body.get('tools'))['seal_result']
    assert not function.get('defer_loading', False)
    assert function['parameters'] == before['_executable_tool_catalog'][0]['function']['parameters']
    assert body == before


@pytest.mark.parametrize('available', [False, True])
def test_revocation_or_revision_uses_current_catalog(monkeypatch, available):
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints', lambda *a, **kw: None)
    body = _body(available=available)
    if available:
        body['_executable_tool_catalog'][0] = _tool('seal_result', description='New live epoch.')
    plan = prepare_request(body, base_url='https://example.test/v1', api_key='test-key')
    functions = _functions(plan.body.get('tools'))
    assert ('seal_result' in functions) is available
    if available:
        assert functions['seal_result']['description'] == 'New live epoch.'


def test_empty_authority_cannot_regrant_stale_direct_tool(monkeypatch):
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints', lambda *a, **kw: None)
    body = _body()
    body['_executable_tool_catalog'] = []
    plan = prepare_request(body, base_url='https://example.test/v1', api_key='test-key')
    assert 'seal_result' not in _functions(plan.body.get('tools'))


def test_duplicate_and_malformed_sidecars_do_not_create_authority():
    critical = _tool('seal_result')
    assert preserve_direct_wire_tools([critical, critical], authority_catalog=[critical],
                                      declared=['seal_result']) == [critical]
    assert preserve_direct_wire_tools([critical], authority_catalog=[],
                                      declared=['seal_result']) == []
    assert preserve_direct_wire_tools([], authority_catalog=[critical],
                                      declared='seal_result') == []


@pytest.mark.parametrize('exposure', ['gateway_only', 'code_api'])
def test_timer_creation_is_visible_on_compacted_model_wire(monkeypatch, exposure):
    from lib.scheduler.tool_defs import TIMER_TOOL_CREATE
    monkeypatch.setattr('lib.llm._sse_core.add_cache_breakpoints', lambda *a, **kw: None)
    body = _body(stale=True)
    body.update(_executable_tool_catalog=[TIMER_TOOL_CREATE],
        _required_direct_tool_names=['timer_create'], _tool_schema_budget_tokens=1,
        _programmatic_tool_calling='auto', _programmatic_eligible_tools=['timer_create'],
        _programmatic_exposure=exposure, _programmatic_tier='program')
    plan = prepare_request(body, base_url='https://example.test/v1', api_key='test-key')
    function = _functions(plan.body.get('tools'))['timer_create']
    assert function['parameters'] == TIMER_TOOL_CREATE['function']['parameters']

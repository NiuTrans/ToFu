"""Browser discovery exposes owner-scoped availability, never execution grants."""
from __future__ import annotations

import json

import pytest

from lib.browser import queue
from lib.browser.availability import browser_extension_availability
from lib.tasks_pkg.handlers import tool_gateway
from lib.tools.browser import BROWSER_TOOL_LIST_TABS, BROWSER_TOOL_PREVIEW_PAGE
from lib.tools.gateway import search_executable_catalog

pytestmark = pytest.mark.unit


def _search(monkeypatch, task, query='browser_list_tabs', namespace=''):
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: None)
    row = {}
    _, content, _ = tool_gateway.handle_search_tools(
        task, {}, 'search_tools', 'browser-search',
        {'query': query, 'namespace': namespace}, 1, row, {}, None, False)
    return json.loads(content), row


def test_disconnected_browser_returns_known_unavailable_and_safe_ui(monkeypatch):
    owners = []
    def clients(*, owner_user_id):
        owners.append(owner_user_id)
        return []
    monkeypatch.setattr(queue, 'get_connected_clients', clients)
    task = {'id': 'browser-task', '_userId': 7,
            '_executable_tool_catalog': [], '_executableToolNamespaceByName': {}}
    result, row = _search(monkeypatch, task, '+browser_list_tabs', 'browser')
    evidence = result['capability_availability'][0]
    assert owners == ['7']
    assert evidence['reason_code'] == 'browser_extension_disconnected'
    assert evidence['tool_name'] == 'browser_list_tabs'
    assert evidence['available'] is False
    assert 'server URL' in evidence['recovery_hint']
    assert 'cannot read' in evidence['recovery_hint']
    assert result['items'] == [] and result['total'] == 0
    assert 'missing_name' not in result
    ui = tool_gateway._search_display_results(result)
    assert ui[0]['type'] == 'tool_catalog_availability'
    assert ui[0]['reasonCode'] == evidence['reason_code']
    assert 'arguments' not in ui[0] and 'toolName' not in ui[0]
    assert row['toolSearchTotal'] == 0
    repeated, _ = _search(monkeypatch, task)
    repeated, _ = _search(monkeypatch, task)
    assert repeated['repeated_query'] == 2
    assert 'server URL' in repeated['notice']


def test_connection_does_not_grant_tools_and_reassembly_recovers(monkeypatch):
    monkeypatch.setattr(queue, 'get_connected_clients', lambda **kw: [{}])
    task = {'id': 'browser-task', '_userId': 7,
            '_executable_tool_catalog': []}
    blocked, _ = _search(monkeypatch, task)
    assert blocked['capability_availability'][0]['reason_code'] == (
        'browser_tools_outside_task_catalog')
    assert blocked['items'] == []
    task['_executable_tool_catalog'] = [BROWSER_TOOL_LIST_TABS]
    ready, _ = _search(monkeypatch, task, '+browser_list_tabs')
    assert ready['items'][0]['name'] == 'browser_list_tabs'
    assert 'capability_availability' not in ready
    assert 'repeated_query' not in ready


def test_ownerless_availability_never_reads_global_clients(monkeypatch):
    def forbidden(**kw):
        raise AssertionError('No registry access without owner')
    monkeypatch.setattr(queue, 'get_connected_clients', forbidden)
    assert browser_extension_availability(0)['reason_code'] == 'browser_owner_required'


def test_foreign_user_connection_does_not_make_owner_available(monkeypatch):
    monkeypatch.setattr(queue, 'get_connected_clients',
                        lambda *, owner_user_id: [{}] if owner_user_id == '8' else [])
    assert browser_extension_availability(8)['available'] is True
    assert browser_extension_availability(7)['available'] is False


def test_exact_miss_returns_no_list_word_substitutes():
    other = {'type': 'function', 'function': {
        'name': 'mcp__xuecheng__list_xtable_views',
        'description': 'List views for a table',
        'parameters': {'type': 'object', 'properties': {}}}}
    for query in ('browser_list_tabs', '+browser_list_tabs'):
        result = search_executable_catalog([other], query)
        assert result['missing_name'] == 'browser_list_tabs'
        assert result['total'] == 0 and result['items'] == []
        assert result['next_cursor'] is None


def test_disclosed_namespace_is_not_reported_as_nonexistent():
    result = search_executable_catalog(
        [BROWSER_TOOL_PREVIEW_PAGE], 'browser', namespace='browser',
        namespace_by_name={'browser_preview_page': 'browser'},
        disclosed_names={'browser_preview_page'})
    assert result['items'] == []
    assert 'available_namespaces' not in result
    assert 'No tool in this task' not in result['notice']
    assert 'already disclosed' in result['notice']


def test_server_preview_does_not_require_extension(monkeypatch):
    def forbidden(**kw):
        raise AssertionError('Preview does not probe the extension')
    monkeypatch.setattr(queue, 'get_connected_clients', forbidden)
    task = {'_userId': 7, '_executable_tool_catalog': [BROWSER_TOOL_PREVIEW_PAGE]}
    result, _ = _search(monkeypatch, task, 'browser_preview_page')
    assert result['items'][0]['name'] == 'browser_preview_page'
    assert 'capability_availability' not in result


def test_model_environment_observes_disconnect_and_reconnect(monkeypatch):
    from lib.tasks_pkg.context_composer import ComposeRequest
    from lib.tasks_pkg.context_composer import _providers as providers
    state = []
    monkeypatch.setattr(queue, 'get_connected_clients', lambda **kw: state[:])
    def blocks():
        return {block.id: block for block in providers.collect_context_blocks(
            [{'role': 'user', 'content': 'inspect UI'}],
            ComposeRequest(user_id=7, has_real_tools=True, model='m',
                           task={'id': 'env-browser-task', '_userId': 7}))}
    disconnected = blocks()
    assert 'no usable extension' in disconnected['environment'].content
    assert 'server URL' in disconnected['environment'].content
    state.append({})
    connected = blocks()
    assert 'connected for this user' in connected['environment'].content
    assert 'server URL' not in connected['environment'].content
    assert disconnected['platform_static'].content == connected['platform_static'].content


def test_failed_probe_is_unknown_not_disconnected(monkeypatch):
    def broken(**kw):
        raise RuntimeError('synthetic registry failure')
    monkeypatch.setattr(queue, 'get_connected_clients', broken)
    evidence = browser_extension_availability(7)
    assert evidence['reason_code'] == 'browser_extension_status_unknown'
    assert evidence['available'] is False
    assert 'server URL' not in evidence['recovery_hint']


def test_unknown_browser_name_stays_unknown_with_connection_evidence(monkeypatch):
    monkeypatch.setattr(queue, 'get_connected_clients', lambda **kw: [])
    result, _ = _search(monkeypatch, {'_userId': 7, '_executable_tool_catalog': []},
                        'browser_invented_action')
    assert result['missing_name'] == 'browser_invented_action'
    assert 'tool_name' not in result['capability_availability'][0]
    assert result['items'] == []


def test_programmatic_search_has_same_browser_availability(monkeypatch):
    monkeypatch.setattr(queue, 'get_connected_clients', lambda **kw: [])
    monkeypatch.setattr(tool_gateway, '_finalize', lambda *a, **kw: None)
    task = {'id': 'browser-program', '_userId': 7, 'model': 'test',
            '_executable_tool_catalog': []}
    _, content, _ = tool_gateway.handle_execute_tools(
        task, {}, 'execute_tools', 'browser-program',
        {'program': 'return catalog.search("browser_list_tabs");'},
        1, {'llmRound': 0}, {'tools': {'programmaticCalling': 'on'}}, None, False)
    program = json.loads(content)['program']
    assert program['status'] == 'ok', program
    result = program['result']
    assert result['capability_availability'][0]['reason_code'] == (
        'browser_extension_disconnected')
    assert result['items'] == []
    assert not task['programRuns'][0]['childCalls']

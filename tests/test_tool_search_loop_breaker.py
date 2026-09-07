"""Tool Search loop-breakers.

Incident anchor: an autonomous-dispatch turn searched the exact name
``project_board_complete`` (absent from that turn's catalog — the
``project_brain_write`` family only builds with a project attached) and got
46 fuzzy part-matches with no signal that the name itself was missing, so the
model re-searched the same keyword every dispatch round (billing-epic loop,
memory billing-wallet-cas-epic-closure-pending). Two breakers:

  * exact-name miss → ``missing_name`` + a notice stating the catalog is
    task-fixed and re-searching cannot help;
  * the Nth identical (namespace, query) search → ``repeated_query`` + an
    escalating stop notice.
"""

from __future__ import annotations

import json

import pytest

from lib.tools.gateway import search_executable_catalog

pytestmark = pytest.mark.unit


def _tool(name, *, description=''):
    return {
        'type': 'function',
        'function': {
            'name': name,
            'description': description or name,
            'parameters': {
                'type': 'object', 'properties': {}, 'required': []},
        },
    }


def _catalog():
    return [
        _tool('todo_write', description='Maintain the CURRENT task checklist.'),
        _tool('produce_report',
              description='Produce a long-form report from a single topic.'),
        _tool('await_agents',
              description='Block this turn until sub-agents complete.'),
        _tool('project_board_read',
              description='Read the project coordination board.'),
    ]


def _xuecheng_catalog():
    return [
        _tool(
            'mcp__xuecheng__read_doc',
            description=(
                '[MCP:xuecheng] Read or summarize a 学城 document as Markdown.')),
        _tool(
            'mcp__xuecheng__search_docs',
            description=(
                '[MCP:xuecheng] Full-text search across 学城 (Xuecheng) docs.')),
        _tool(
            'mcp__xuecheng__login',
            description=(
                '[MCP:xuecheng] Log in after a data tool returns '
                'NOT_LOGGED_IN. May send an approval push.')),
    ]


def test_exact_name_miss_is_called_out_instead_of_inviting_research():
    result = search_executable_catalog(_catalog(), 'project_board_complete')

    assert result['status'] == 'ok'
    assert result['missing_name'] == 'project_board_complete'
    assert 'project_board_complete' not in {
        row['name'] for row in result['items']}
    assert 'cannot make it appear' in result['notice']
    assert 'Do not search for this name again' in result['notice']


def test_exact_name_hit_ranks_first_and_carries_no_missing_flag():
    result = search_executable_catalog(_catalog(), 'project_board_read')

    assert 'missing_name' not in result
    assert result['items'][0]['name'] == 'project_board_read'
    assert result['notice'].startswith('Call execute_tools')


def test_natural_language_query_is_never_a_name_lookup():
    result = search_executable_catalog(
        _catalog(), 'close the finished board epic')

    assert 'missing_name' not in result
    assert result['notice'].startswith('Call execute_tools')


def test_exact_name_in_other_namespace_gets_hint_not_absence():
    namespaces = {
        'project_board_read': 'conversation', 'todo_write': 'task',
        'produce_report': 'video', 'await_agents': 'swarm',
    }
    result = search_executable_catalog(
        _catalog(), 'project_board_read', namespace='task',
        namespace_by_name=namespaces)

    assert 'missing_name' not in result
    assert "'conversation'" in result['notice']


def test_repeated_identical_search_escalates_a_loop_breaking_notice(
        monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'id': 'task-search-loop', 'model': 'test',
        '_executable_tool_catalog': _catalog(),
        '_executableToolNamespaceByName': {},
    }

    def run():
        _tc_id, content, _aborted = handler.handle_search_tools(
            task, {}, 'search_tools', 'call-1',
            {'query': 'project_board_complete'}, 1, {}, {}, None, False)
        return json.loads(content)

    first = run()
    assert first['missing_name'] == 'project_board_complete'
    assert 'repeated_query' not in first

    second = run()
    assert second['repeated_query'] == 2
    assert 'the outcome cannot change' in second['notice']

    third = run()
    assert third['repeated_query'] == 3


def test_distinct_queries_do_not_trip_the_repeat_breaker(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'id': 'task-search-varied', 'model': 'test',
        '_executable_tool_catalog': _catalog(),
        '_executableToolNamespaceByName': {},
    }

    for query in ('project_board_complete', 'board epic', 'close task'):
        _tc_id, content, _aborted = handler.handle_search_tools(
            task, {}, 'search_tools', 'call-1', {'query': query},
            1, {}, {}, None, False)
        assert 'repeated_query' not in json.loads(content)


def _claim_catalog():
    claim = _tool(
        'project_board_claim',
        description='Claim an OPEN epic before you start working it.')
    claim['function']['parameters'] = {
        'type': 'object',
        'properties': {'task_id': {'type': 'string'}},
        'required': ['task_id'],
    }
    return [
        claim,
        _tool('project_board_read',
              description='Read the project coordination board.'),
    ]


def test_disclosed_exact_lookup_re_returns_schema_for_execute_tools():
    # Incident anchor: a disclosed-only tool (schema returned by an earlier
    # search, then lost to compaction) hit the already_visible branch, whose
    # notice claimed the tool was directly callable and returned no schema —
    # the model guessed epic_id instead of task_id (mtgzs6bnmglpfa).
    result = search_executable_catalog(
        _claim_catalog(), 'project_board_claim',
        disclosed_names={'project_board_claim'})

    assert result['already_disclosed'] == 'project_board_claim'
    assert 'already_visible' not in result
    assert [row['name'] for row in result['items']] == ['project_board_claim']
    schema = result['items'][0]['arguments_schema']
    assert schema['required'] == ['task_id']
    assert 'execute_tools' in result['notice']
    assert 'NOT in your direct tool list' in result['notice']
    assert 'Do not search for this name again' in result['notice']


def test_disclosed_exact_lookup_ignores_a_wrong_namespace_filter():
    result = search_executable_catalog(
        _claim_catalog(), 'project_board_claim', namespace='project',
        namespace_by_name={'project_board_claim': 'conversation',
                           'project_board_read': 'conversation'},
        disclosed_names={'project_board_claim'})

    assert result['already_disclosed'] == 'project_board_claim'
    assert [row['name'] for row in result['items']] == ['project_board_claim']


def test_disclosed_tool_stays_omitted_from_broad_results():
    result = search_executable_catalog(
        _claim_catalog(), 'project board epic',
        disclosed_names={'project_board_claim'})

    names = {row['name'] for row in result['items']}
    assert 'project_board_claim' not in names
    assert 'project_board_read' in names


def test_zero_result_search_points_at_omitted_disclosed_tools():
    result = search_executable_catalog(
        _claim_catalog(), 'nonexistent capability xyz',
        disclosed_names={'project_board_claim'})

    assert result['items'] == []
    assert 'already disclosed' in result['notice']


def test_wire_visible_wins_over_disclosed_for_exact_lookup():
    result = search_executable_catalog(
        _claim_catalog(), 'project_board_claim',
        visible_names={'project_board_claim'},
        disclosed_names={'project_board_claim'})

    assert result['already_visible'] == 'project_board_claim'
    assert 'already_disclosed' not in result
    assert result['items'] == []


def test_handler_re_discloses_schema_lost_to_compaction(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    task = {
        'id': 'task-redisclose', 'model': 'test',
        '_executable_tool_catalog': _claim_catalog(),
        '_executableToolNamespaceByName': {},
    }

    def run(query):
        _tc_id, content, _aborted = handler.handle_search_tools(
            task, {}, 'search_tools', 'call-1', {'query': query},
            1, {}, {}, None, False)
        return json.loads(content)

    first = run('project board epic')
    assert 'project_board_claim' in {row['name'] for row in first['items']}

    second = run('project_board_claim')
    assert second['already_disclosed'] == 'project_board_claim'
    assert second['items'][0]['arguments_schema']['required'] == ['task_id']

    third = run('board coordination')
    assert third['items'] == []
    assert 'already disclosed' in third['notice']


def test_visible_tool_is_omitted_and_exact_lookup_says_call_it_directly():
    visible = {'mcp__xuecheng__read_doc'}

    broad = search_executable_catalog(
        _xuecheng_catalog(), 'xuecheng document authorization',
        visible_names=visible)
    assert 'mcp__xuecheng__read_doc' not in {
        row['name'] for row in broad['items']}

    exact = search_executable_catalog(
        _xuecheng_catalog(), 'mcp__xuecheng__read_doc',
        visible_names=visible)
    assert exact['items'] == []
    assert exact['already_visible'] == 'mcp__xuecheng__read_doc'
    assert 'call it directly' in exact['notice'].lower()
    assert 'missing_name' not in exact


def test_handler_uses_final_wire_projection_not_broader_assembly_schema(
        monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    catalog = _xuecheng_catalog()
    task = {
        'id': 'task-visible-tools', 'model': 'test',
        '_executable_tool_catalog': catalog,
        # Assembly proposed both tools, but the provider budget retained only
        # read_doc. The final wire projection is the visibility authority.
        '_tool_schema': catalog[:2],
        'events': [{
            'type': 'tool_wire_projection', 'roundNum': 2,
            'toolNames': [
                'mcp__xuecheng__read_doc', 'search_tools', 'execute_tools'],
        }],
    }

    _tc_id, content, _aborted = handler.handle_search_tools(
        task, {}, 'search_tools', 'call-visible',
        {'query': 'xuecheng document'}, 2, {'llmRound': 2}, {}, None, False)

    names = {row['name'] for row in json.loads(content)['items']}
    assert 'mcp__xuecheng__read_doc' not in names
    assert 'mcp__xuecheng__search_docs' in names


def test_fail_open_directory_still_omits_visible_tools(monkeypatch):
    from lib.tasks_pkg.handlers import tool_gateway as handler

    monkeypatch.setattr(handler, '_finalize', lambda *args, **kwargs: None)
    monkeypatch.setattr(
        handler, 'search_executable_catalog',
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('boom')))
    catalog = _xuecheng_catalog()
    task = {
        'id': 'task-visible-fail-open', 'model': 'test',
        '_executable_tool_catalog': catalog,
        '_tool_schema': [catalog[0]],
    }

    _tc_id, content, _aborted = handler.handle_search_tools(
        task, {}, 'search_tools', 'call-fail-open',
        {'query': 'xuecheng'}, 2, {'llmRound': 2}, {}, None, False)

    result = json.loads(content)
    assert result['fail_open'] is True
    assert 'mcp__xuecheng__read_doc' not in {
        row['name'] for row in result['items']}
    assert 'mcp__xuecheng__search_docs' in {
        row['name'] for row in result['items']}


def test_authentication_intent_ranks_login_ahead_of_document_search():
    for query in ('authorize access to xuecheng docs', '学城登录授权'):
        result = search_executable_catalog(_xuecheng_catalog(), query)
        assert result['items'][0]['name'] == 'mcp__xuecheng__login'

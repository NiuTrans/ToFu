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

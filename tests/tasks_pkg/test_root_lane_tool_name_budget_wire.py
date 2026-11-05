"""Task tool metadata -> final wire on the your-llm-gateway.example.com Anthropic face.

The root/general surface keeps its task-owned execution authority while the
provider wire hides the longest optional names to stay under the gateway's
cache-affinity name budget (see tests/cache/test_cache_tool_name_budget_wire.py).
Every hidden tool must stay reachable through search_tools -> execute_tools.
"""
from copy import deepcopy

import pytest

from lib.llm import tool_wire
from lib.tools.gateway import search_executable_catalog
from tests.llm.test_tool_wire_name_budget import (
    CODE_FLOOR, ROOT_WIRE_NAMES, SANKUAI_ANTHROPIC, _joined_bytes, _names, _tool)

pytestmark = pytest.mark.unit

_SEARCHABLE = ('send_agent_message', 'get_agent_messages', 'update_memory')


def _root_task():
    visible = [_tool(name) for name in ROOT_WIRE_NAMES
               if name not in ('search_tools', 'execute_tools')]
    catalog = deepcopy(visible) + [_tool(name) for name in _SEARCHABLE]
    policy = {tool['function']['name']: 'eager' for tool in visible}
    policy.update({name: 'searchable' for name in _SEARCHABLE})
    return {
        'id': 'root-lane-name-budget', '_userId': 7, 'status': 'running',
        'config': {},
        '_executable_tool_catalog': catalog,
        '_frontendSelectedToolNames': [
            name for name in ROOT_WIRE_NAMES if name.startswith('mcp__')
        ] + ['poll', 'kill'],
        '_requiredDirectToolNames': list(CODE_FLOOR),
        '_toolDiscoveryPolicyByName': policy,
        '_toolSearchableCount': len(_SEARCHABLE),
        '_toolSearchMode': 'local',
    }, visible


def _project(task, visible, base_url):
    from lib.tasks_pkg.orchestrator._tool_request_metadata import (
        attach_tool_request_metadata)
    messages = [{'role': 'user', 'content': 'continue the investigation'}]
    body = {'model': 'claude-opus-5.5', 'messages': messages,
            'tools': deepcopy(visible)}
    attach_tool_request_metadata(task, body, messages, visible,
                                 model=body['model'], round_num=3, tid=task['id'])
    projection = tool_wire.project_tool_wire(
        body, api_protocol='anthropic', base_url=base_url)
    return body, projection


def test_hidden_root_tools_stay_discoverable_and_executable():
    task, visible = _root_task()
    body, projection = _project(task, visible, SANKUAI_ANTHROPIC)
    wire_names = _names(body['tools'])
    hidden = [name for name in ROOT_WIRE_NAMES if name not in wire_names]

    assert _joined_bytes(wire_names) <= tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES
    assert hidden and set(hidden) <= set(projection.budget_dropped_names)
    assert set(CODE_FLOOR) | {'poll', 'kill', 'search_tools', 'execute_tools'} <= set(wire_names)
    assert _names(task['_executable_tool_catalog']) == _names(
        body['_executable_tool_catalog'])
    for name in hidden:
        found = search_executable_catalog(
            body['_executable_tool_catalog'], name,
            visible_names=set(wire_names))
        assert found['status'] == 'ok'
        assert found['execute_with'] == 'execute_tools'
        assert name in [item['name'] for item in found['items']]


def test_other_hosts_receive_the_task_surface_unchanged():
    task, visible = _root_task()
    body, projection = _project(task, visible, 'https://api.anthropic.com/v1')
    # The default program tier adds its code action to the unchanged task surface.
    assert set(_names(body['tools'])) == set(ROOT_WIRE_NAMES) | {'execute_program'}
    assert not projection.budget_dropped_names

"""Tool-name byte budget for Claude on the your-llm-gateway.example.com Anthropic face.

Live A/B on 2026-10-28 (kunlunbedrock.claude-opus-5.5-m17, byte-identical
bodies sent 10-15 s apart): the gateway keeps prompt-cache affinity only while
``sum(len(name) + 1)`` over the request's tool names stays at or below ~492
bytes. Names alone decide it; schema size, tool count, descriptions, order and
the system prompt do not. Above it every round re-writes the whole prefix.
Kimi on the same face reads ~98% with root-sized surfaces and is not budgeted.
"""
from copy import deepcopy

import pytest

from lib.llm import tool_wire

pytestmark = pytest.mark.unit

SANKUAI_ANTHROPIC = 'https://api.openai.com/v1/anthropic'

# Joined-name bytes of probes that did / did not read their own write back
# (35-tool probes; 10- and 20-tool probes bracket the same boundary).
MEASURED_LAST_HIT_BYTES = 492
MEASURED_FIRST_MISS_BYTES = 496

# Final provider tool names of production root round R61 (task e1573a09,
# 2026-10-28): 606 joined bytes, cache_read=0 on every round.
ROOT_WIRE_NAMES = (
    'web_search', 'fetch_url', 'read_files', 'inspect_image', 'search_files',
    'find_files', 'repo_map', 'write_file', 'edit_file', 'run_command',
    'list_conversations', 'get_conversation', 'browser_download_url_to_server',
    'create_memory', 'search_memories', 'search_skills', 'load_skill',
    'read_skill_resource', 'timer_create', 'read_tool_artifact',
    'search_tool_artifact', 'spawn_agents', 'get_agent_result',
    'mcp__xuecheng__clear_auth_cache', 'mcp__hope__get_queue_resource',
    'mcp__github__search_issues', 'mcp__github__list_issues',
    'mcp__xuecheng__update_doc_section', 'mcp__llm__experiment_eval_jobs',
    'mcp__overleaf__locate_in_pdf', 'mcp__github__push_files', 'poll', 'kill',
    'search_tools', 'execute_tools',
)
CODE_FLOOR = ('read_files', 'search_files', 'find_files', 'edit_file', 'run_command')


def _tool(name):
    return {'type': 'function', 'function': {
        'name': name, 'description': f'{name} tool.',
        'parameters': {'type': 'object', 'properties': {}}}}


def _names(tools):
    return [tool_wire.provider_tool_name(tool) for tool in tools]


def _captured_warnings(monkeypatch):
    warnings = []
    monkeypatch.setattr(tool_wire.logger, 'warning',
                        lambda fmt, *args: warnings.append(fmt % args))
    return warnings


def _joined_bytes(names):
    return sum(len(name.encode('utf-8')) + 1 for name in names)


def test_budget_binds_only_claude_on_the_example_corp_anthropic_face():
    budget = tool_wire.provider_tool_name_budget_bytes
    claude = 'kunlunbedrock.claude-opus-5.5-m17'
    assert budget(api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC,
                  model=claude) == tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES
    assert budget(api_protocol='anthropic', model='claude-opus-5.5',
                  base_url='https://api.openai.com/v1/anthropic/') > 0
    assert budget(api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC,
                  model='kimi-k3.1-pro') == 0
    for protocol, url in (
            ('openai', 'https://api.openai.com/v1'),
            ('responses', 'https://api.openai.com/v1'),
            ('anthropic', 'https://api.anthropic.com/v1'),
            ('anthropic', 'https://mcli.internal.example.com/anthropic'),
            ('anthropic', ''), ('anthropic', 'http://[::1')):
        assert budget(api_protocol=protocol, base_url=url, model=claude) == 0
    assert _joined_bytes(ROOT_WIRE_NAMES) >= MEASURED_FIRST_MISS_BYTES
    assert (tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES + 32
            <= MEASURED_LAST_HIT_BYTES)


def test_root_surface_hides_longest_optional_names_until_it_fits():
    tools = [_tool(name) for name in ROOT_WIRE_NAMES]
    floor = {'search_tools', 'execute_tools', 'poll', 'kill', 'spawn_agents',
             'get_agent_result', *CODE_FLOOR}
    fitted = tool_wire.fit_tool_name_budget(
        tools, budget_bytes=448, required_names=floor)
    kept = _names(fitted)
    hidden = [name for name in ROOT_WIRE_NAMES if name not in kept]

    assert _joined_bytes(kept) <= 448
    assert floor <= set(kept)
    assert kept == [name for name in ROOT_WIRE_NAMES if name in kept]
    # Hiding the longest names keeps the largest number of direct tools.
    assert sorted(hidden) == sorted([
        'mcp__xuecheng__update_doc_section', 'mcp__xuecheng__clear_auth_cache',
        'mcp__llm__experiment_eval_jobs', 'browser_download_url_to_server',
        'mcp__hope__get_queue_resource'])
    assert min(map(len, hidden)) >= max(
        len(name) for name in kept if name not in floor)
    assert all(tool is original for tool, original in zip(
        fitted, [t for t in tools if _names([t])[0] in kept]))


def test_within_budget_and_ties_are_deterministic():
    tools = [_tool(name) for name in ('aaaa', 'bbbb', 'cccc', 'search_tools')]
    assert tool_wire.fit_tool_name_budget(
        tools, budget_bytes=100, required_names=set()) is tools
    # Equal lengths hide the later wire position first.
    fitted = tool_wire.fit_tool_name_budget(
        tools, budget_bytes=_joined_bytes(['aaaa', 'search_tools']) + 2,
        required_names={'search_tools'})
    assert _names(fitted) == ['aaaa', 'search_tools']


def test_required_floor_survives_even_above_the_budget():
    tools = [_tool(name) for name in ('read_files', 'mcp__a__long_tool_name',
                                       'search_tools', 'execute_tools')]
    fitted = tool_wire.fit_tool_name_budget(
        tools, budget_bytes=8,
        required_names={'read_files', 'search_tools', 'execute_tools'})
    assert _names(fitted) == ['read_files', 'search_tools', 'execute_tools']


def _root_body(**extra):
    catalog = [_tool(name) for name in ROOT_WIRE_NAMES
               if name not in ('search_tools', 'execute_tools')]
    body = {
        'model': 'kunlunbedrock.claude-opus-5.5-m17',
        'messages': [{'role': 'user', 'content': 'continue'}],
        'tools': deepcopy(catalog),
        '_executable_tool_catalog': deepcopy(catalog),
        '_tool_wire_catalog': deepcopy(catalog),
        '_tool_search_mode': 'local',
        '_tool_search_catalog_size': 350,
        '_tool_searchable_count': 300,
        '_tool_discovery_policy_by_name': {
            tool['function']['name']: 'eager' for tool in catalog},
        '_required_direct_tool_names': list(CODE_FLOOR),
    }
    body.update(extra)
    return body


def test_projection_fits_example_corp_anthropic_and_keeps_discovery_live():
    body = _root_body()
    projection = tool_wire.project_tool_wire(
        body, api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC)
    names = _names(body['tools'])
    hidden = [name for name in ROOT_WIRE_NAMES if name not in names]

    assert _joined_bytes(names) <= tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES
    assert {'search_tools', 'execute_tools', 'poll', 'kill', *CODE_FLOOR} <= set(names)
    assert hidden and set(hidden) <= set(projection.budget_dropped_names)
    # Hidden tools keep their execution authority for search/execute_tools.
    assert set(hidden) <= set(_names(body['_executable_tool_catalog']))

    again = _root_body()
    tool_wire.project_tool_wire(
        again, api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC)
    assert again['tools'] == body['tools']
    reprojected = dict(body, _tool_wire_catalog=deepcopy(body['tools']))
    tool_wire.project_tool_wire(
        reprojected, api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC)
    assert _names(reprojected['tools']) == names


@pytest.mark.parametrize('protocol,url,model', [
    ('openai', 'https://api.openai.com/v1', 'claude-opus-5.5'),
    ('anthropic', 'https://api.anthropic.com/v1', 'claude-opus-5.5'),
    ('anthropic', SANKUAI_ANTHROPIC, 'kimi-k3.1-pro'),
])
def test_other_routes_keep_the_complete_eager_surface(protocol, url, model):
    body = _root_body(model=model)
    projection = tool_wire.project_tool_wire(
        body, api_protocol=protocol, base_url=url)
    assert set(_names(body['tools'])) == set(ROOT_WIRE_NAMES)
    assert not projection.budget_dropped_names


def test_no_demotion_without_a_discovery_path(monkeypatch):
    warnings = _captured_warnings(monkeypatch)
    body = _root_body(_tool_searchable_count=0, _tool_search_catalog_size=0)
    tool_wire.project_tool_wire(
        body, api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC)
    names = _names(body['tools'])
    assert 'search_tools' not in names
    assert set(names) == set(ROOT_WIRE_NAMES) - {'search_tools', 'execute_tools'}
    # A request that cannot fit still goes out, but never silently.
    (warning,) = warnings
    assert 'NOT met' in warning and 'no search_tools/execute_tools' in warning
    assert f'{_joined_bytes(names)} bytes sent' in warning


def test_explicit_tool_search_off_is_honored_and_reported(monkeypatch):
    warnings = _captured_warnings(monkeypatch)
    body = _root_body(_tool_search_mode='off')
    projection = tool_wire.project_tool_wire(
        body, api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC,
        log_prefix='[task]')
    # ``off`` keeps every tool directly callable; nothing is hidden.
    assert set(_names(body['tools'])) == set(ROOT_WIRE_NAMES) - {
        'search_tools', 'execute_tools'}
    assert not projection.budget_dropped_names
    assert (projection.tool_name_budget_bytes
            == tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES)
    (warning,) = warnings
    assert warning.startswith('[task] [ToolWire] tool-name budget 448 bytes NOT met')
    assert 'backend=full' in warning


def test_fitting_and_unbudgeted_surfaces_never_warn(monkeypatch):
    warnings = _captured_warnings(monkeypatch)
    tool_wire.project_tool_wire(
        _root_body(), api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC)
    projection = tool_wire.project_tool_wire(
        _root_body(model='kimi-k3.1-pro', _tool_search_mode='off'),
        api_protocol='anthropic', base_url=SANKUAI_ANTHROPIC)
    assert projection.tool_name_budget_bytes == 0
    assert warnings == []


def test_never_hidden_names_leave_room_under_the_route_budget():
    from lib.swarm.tools import GET_AGENT_RESULT_TOOL, SPAWN_AGENTS_TOOL
    from lib.tools.gateway import CODE_CORE_DIRECT_TOOL_NAMES, GATEWAY_TOOL_NAMES
    from lib.tools.poll import BACKGROUND_TOOL_NAMES

    never_hidden = (set(GATEWAY_TOOL_NAMES) | set(BACKGROUND_TOOL_NAMES)
                    | set(CODE_CORE_DIRECT_TOOL_NAMES)
                    | set(_names([SPAWN_AGENTS_TOOL, GET_AGENT_RESULT_TOOL])))
    assert 'spawn_agents' in never_hidden
    # Growing this floor toward the budget would leave no room for optional
    # tools and push every root request back over the cache-affinity edge.
    assert (_joined_bytes(never_hidden)
            <= tool_wire.SANKUAI_ANTHROPIC_TOOL_NAME_BUDGET_BYTES // 2)


def test_tool_name_bytes_counts_provider_visible_names():
    tools = [_tool('read_files'), {'name': 'mcp__a__b'}, {'type': 'web_search'},
             None]
    assert tool_wire.tool_name_bytes(tools) == _joined_bytes(
        ['read_files', 'mcp__a__b'])
    assert tool_wire.tool_name_bytes(None) == 0

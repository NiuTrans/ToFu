"""tests/swarm/test_swarm_tool_scoping.py — ensure sub-agents can never see
swarm-control tools (spawn_agents / await_agents / get_agent_result)
or ask_human, regardless of role.
"""

import pytest

from lib.swarm.registry import AGENT_ROLES, scope_tools_for_role
from lib.swarm.tools import (
    AWAIT_AGENTS_TOOL,
    GET_AGENT_RESULT_TOOL,
    SPAWN_AGENTS_TOOL,
    SUB_AGENT_DENYLIST,
)


pytestmark = pytest.mark.unit


def _names(tools):
    return [t.get('function', {}).get('name', '') for t in tools]


def _make_full_tool_list():
    """A representative tool list mixing master-only, denylisted, and ordinary tools."""
    ask_human_stub = {
        "type": "function",
        "function": {"name": "ask_human", "description": "...", "parameters": {}},
    }
    web_search_stub = {
        "type": "function",
        "function": {"name": "web_search", "description": "...", "parameters": {}},
    }
    read_files_stub = {
        "type": "function",
        "function": {"name": "read_files", "description": "...", "parameters": {}},
    }
    grep_stub = {
        "type": "function",
        "function": {"name": "search_files", "description": "...", "parameters": {}},
    }
    fetch_stub = {
        "type": "function",
        "function": {"name": "fetch_url", "description": "...", "parameters": {}},
    }
    return [
        SPAWN_AGENTS_TOOL,
        AWAIT_AGENTS_TOOL,
        GET_AGENT_RESULT_TOOL,
        ask_human_stub,
        web_search_stub,
        fetch_stub,
        read_files_stub,
        grep_stub,
    ]


@pytest.mark.parametrize('role', sorted(AGENT_ROLES.keys()))
def test_sub_agent_never_sees_denylist(role):
    """For EVERY defined role, swarm-control + ask_human must be stripped."""
    full = _make_full_tool_list()
    scoped = scope_tools_for_role(role, full)
    scoped_names = set(_names(scoped))

    for forbidden in SUB_AGENT_DENYLIST:
        assert forbidden not in scoped_names, (
            f'role={role!r} leaked {forbidden!r} into sub-agent tool list')


def test_general_role_keeps_all_safe_tools():
    """A 'general' sub-agent should keep web_search / read_files / etc., minus the denylist."""
    full = _make_full_tool_list()
    scoped = scope_tools_for_role('general', full)
    names = set(_names(scoped))
    assert 'web_search' in names
    assert 'read_files' in names
    assert 'fetch_url' in names
    # And nothing from the denylist
    assert names.isdisjoint(SUB_AGENT_DENYLIST)


def test_unknown_role_falls_back_to_general_then_strips_denylist():
    """An unknown role should be safe (fallback to 'general' then strip)."""
    full = _make_full_tool_list()
    scoped = scope_tools_for_role('totally_made_up_role', full)
    names = set(_names(scoped))
    assert names.isdisjoint(SUB_AGENT_DENYLIST)


def test_coder_role_excludes_denylist_even_when_hint_empty_match():
    """Even role-scoped (coder) lists must strip swarm tools."""
    full = _make_full_tool_list()
    scoped = scope_tools_for_role('coder', full)
    names = set(_names(scoped))
    assert 'read_files' in names
    assert 'search_files' in names
    assert names.isdisjoint(SUB_AGENT_DENYLIST)


def test_explicit_allowlist_keeps_renamed_tool_without_widening():
    """A spawn allowlist naming the retired ``grep_search`` still selects
    ``search_files``, but only where the role scope already keeps it."""
    from lib.swarm.protocol import SubTaskSpec
    from lib.swarm.registry import scope_tools_for_spec

    full = _make_full_tool_list()
    coder = SubTaskSpec(role='coder', objective='x',
                        tools=['read_files', 'grep_search'])
    assert set(_names(scope_tools_for_spec(coder, full))) == {
        'read_files', 'search_files'}
    researcher = SubTaskSpec(role='researcher', objective='x',
                             tools=['grep_search'])
    assert _names(scope_tools_for_spec(researcher, full)) == []


def _stub(name):
    return {'type': 'function',
            'function': {'name': name, 'description': '...', 'parameters': {}}}


def test_explicit_run_command_allowlist_keeps_its_command_controls():
    """Narrowing to run_command keeps a backgrounded command collectable and
    stoppable; it never grants controls the role scope removed."""
    from lib.swarm.protocol import SubTaskSpec
    from lib.swarm.registry import scope_tools_for_spec

    full = _make_full_tool_list() + [_stub('run_command'), _stub('poll'), _stub('kill')]
    coder = SubTaskSpec(role='coder', objective='x', tools=['run_command'])
    assert set(_names(scope_tools_for_spec(coder, full))) == {'run_command', 'poll', 'kill'}
    reader = SubTaskSpec(role='coder', objective='x', tools=['read_files'])
    assert _names(scope_tools_for_spec(reader, full)) == ['read_files']
    researcher = SubTaskSpec(role='researcher', objective='x', tools=['run_command'])
    assert _names(scope_tools_for_spec(researcher, full)) == []


@pytest.mark.parametrize('role,commands', [
    ('coder', True), ('analyst', True), ('reviewer', True), ('general', True),
    ('researcher', False), ('writer', False),
])
def test_worker_catalog_from_the_real_registry_carries_scoped_command_controls(role, commands):
    """Registry catalog -> SubAgent role scope -> child tool assembly -> wire."""
    from lib.swarm.agent import SubAgent
    from lib.swarm.protocol import SubTaskSpec
    from lib.tasks_pkg.child_invocation import create_child_invocation
    from lib.tasks_pkg.manager import discard_task
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    from lib.tools.gateway import preserve_background_wire_tools
    from lib.tools.registry import ToolContext, assemble_tool_list

    ctx = ToolContext(
        cfg={}, task_id='worker-catalog', project_path='/workspace',
        project_enabled=True, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, enabled_plugins=set())
    assemble_tool_list(ctx)
    parent = {'id': 'coordinator-task', 'convId': 'coordinator-conv', '_userId': 7,
              '_transientRuntime': True,
              'config': {'memoryEnabled': False, 'searchMode': 'off'}}
    agent = SubAgent(SubTaskSpec(role=role, objective='Run the suite'), parent_task=parent,
                     all_tools=ctx.executable_tool_catalog, model='fixture-model')
    controls = {'run_command', 'poll', 'kill'} & set(_names(agent.tools))
    assert controls == ({'run_command', 'poll', 'kill'} if commands else set())
    if not commands:
        return
    child = create_child_invocation(
        parent, model='fixture-model', tools=agent.tools, objective='Run the suite',
        event_sink=lambda event: None, abort_check=lambda: False)
    try:
        tools, _ = assemble_round_tools(dict(child['config']), child, {})
        catalog = child['_executable_tool_catalog']
        wire = preserve_background_wire_tools(tools, authority_catalog=catalog)
        for surface in (catalog, wire):
            functions = {tool['function']['name']: tool['function'] for tool in surface}
            assert {'run_command', 'poll', 'kill'} <= set(functions)
            poll, kill = functions['poll']['description'], functions['kill']['description']
            assert 'background shell commands you started' in poll
            assert 'swarm sub-agents' not in poll and 'other conversations' not in poll
            assert 'a bg_* background command you started' in kill
            assert 'swarm sub-agent' not in kill
            background = functions['run_command']['parameters']['properties'][
                'background']['description']
            assert '`poll`' in background and '`kill`' in background
            assert 'Nothing delivers its completion to this task automatically' in background
    finally:
        discard_task(child['id'])


def test_browser_role_exposes_declared_download_without_privileged_tools():
    """Browser download is role-visible without expanding to shell or writes."""
    tools = [
        {'type': 'function',
         'function': {'name': 'browser_preview_page', 'parameters': {}}},
        {'type': 'function',
         'function': {
             'name': 'browser_download_url_to_server',
             'parameters': {},
         }},
        {'type': 'function',
         'function': {'name': 'run_command', 'parameters': {}}},
        {'type': 'function',
         'function': {'name': 'write_file', 'parameters': {}}},
    ]
    names = set(_names(scope_tools_for_role('browser', tools)))
    assert names == {
        'browser_preview_page',
        'browser_download_url_to_server',
    }

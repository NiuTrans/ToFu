"""tests/test_swarm_tool_scoping.py — ensure sub-agents can never see
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
        "function": {"name": "grep_search", "description": "...", "parameters": {}},
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
    assert 'grep_search' in names
    assert names.isdisjoint(SUB_AGENT_DENYLIST)


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

"""Explicit tool group policies refuse typos before execution can spend."""
from __future__ import annotations

import pytest

from lib.tools.registry import ToolContext, ToolSpec, ToolSpecSelectionError, assemble_tool_list

pytestmark = pytest.mark.unit


def context(cfg):
    return ToolContext(cfg={**cfg, 'tools':{'nativeExposure':'full'}}, task_id='tool-policy-test', project_path='',
        project_enabled=False, search_mode='off', search_enabled=False,
        fetch_enabled=False, code_exec_enabled=False, browser_enabled=False,
        desktop_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[])


@pytest.fixture
def registry(monkeypatch):
    import lib.tools.registry._spec as owner
    calls = []
    def build(key):
        def invoke(ctx):
            calls.append(key)
            return [{'type':'function','function':{'name':key+'_tool',
                'description':'Owned test capability.',
                'parameters':{'type':'object','properties':{}}}}]
        return invoke
    monkeypatch.setattr(owner, '_TOOL_SPECS', [
        ToolSpec('project', build('project'), provides=frozenset({'run_command'})),
        ToolSpec('gated', build('gated'), exposure_gate=lambda ctx:False),
    ])
    return calls


@pytest.mark.parametrize('cfg', [
    {'toolSpecAllowlist':['run_command']},
    {'toolSpecAllowlist':['project','missing']},
    {'toolSpecDenylist':['missing']},
    {'toolSpecAllowlist':'project'},
    {'toolSpecDenylist':None},
    {'toolSpecAllowlist':[None]},
    {'toolSpecAllowlist':[{}]},
    {'toolSpecDenylist':[' ']},
])
def test_invalid_policy_prevents_building_any_capability(registry, cfg):
    ctx = context(cfg)
    with pytest.raises(ToolSpecSelectionError):
        assemble_tool_list(ctx)
    assert not registry and not ctx.executable_tool_catalog


def test_function_name_error_identifies_group_without_granting_it(registry):
    with pytest.raises(ToolSpecSelectionError, match='run_command uses registry key project'):
        assemble_tool_list(context({'toolSpecAllowlist':['run_command']}))
    assert not registry


@pytest.mark.parametrize('cfg, expected', [
    ({'toolSpecAllowlist':[]}, []),
    ({'toolSpecAllowlist':None}, ['project_tool']),
    ({'toolSpecAllowlist':['project','project']}, ['project_tool']),
    ({'toolSpecAllowlist':['gated']}, []),
    ({'toolSpecDenylist':['project']}, []),
])
def test_valid_policies_preserve_gates_and_empty_selection(registry, cfg, expected):
    tools, _ = assemble_tool_list(context(cfg))
    assert [tool['function']['name'] for tool in tools] == expected


def test_invalid_task_policy_propagates_and_clears_stale_authority(monkeypatch):
    from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools
    task = {'id':'policy-failure','messages':[],
        '_executable_tool_catalog':[{'stale':True}],
        '_toolContractDocumentsByName':{'stale':{'authority':True}},
        '_tool_schema':[{'stale':True}]}
    mcfg = dict(project_path='', project_enabled=False, search_mode='off',
        search_enabled=False, fetch_enabled=False, code_exec_enabled=False,
        browser_enabled=False, desktop_enabled=False, image_gen_enabled=False)
    with pytest.raises(ToolSpecSelectionError):
        assemble_round_tools({'toolSpecAllowlist':['run_command']}, task, mcfg)
    assert task['_executable_tool_catalog'] == []
    assert task['_toolContractDocumentsByName'] == {}
    assert task['_tool_schema'] == []

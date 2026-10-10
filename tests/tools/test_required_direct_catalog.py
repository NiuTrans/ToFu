"""Use registry builders and actual task assembly, including revocation."""
import pytest

from lib.tasks_pkg.orchestrator._tool_assembly_prep import assemble_round_tools, _disable_tools_for_turn
from lib.tools.registry import all_specs

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('stage,project,expected', [
    ('executing', True, ['evolution_artifact']),
    ('reviewing', True, []),
    ('executing', False, []),
])
def test_real_artifact_spec_carries_only_live_direct_declarations(monkeypatch, stage, project, expected):
    import lib.tools.registry._spec as owner
    spec = next(spec for spec in all_specs() if spec.key == 'evolution_artifact')
    monkeypatch.setattr(owner, '_TOOL_SPECS', [spec])
    cfg = {'projectEnabled': project, 'projectPath': '/tmp',
           'evolution': {'run_id': 'run-test', 'goal_id': 'goal-test', 'stage': stage,
                         'project_path': '/tmp'}}
    task = {'id': 'task-test', 'config': cfg, 'messages': [], '_userId': 1}
    mcfg = {'project_path': '/tmp', 'project_enabled': project, 'search_mode': 'off',
            'search_enabled': False, 'fetch_enabled': False, 'code_exec_enabled': False,
            'browser_enabled': False, 'desktop_enabled': False, 'image_gen_enabled': False}
    assemble_round_tools(cfg, task, mcfg)
    assert cfg['_requiredDirectToolNames'] == expected
    assert task['_requiredDirectToolNames'] == expected
    assert {tool['function']['name'] for tool in task['_executable_tool_catalog']} == set(expected)
    if expected:
        cfg['evolution']['stage'] = 'reviewing'
        assemble_round_tools(cfg, task, mcfg)
        assert not cfg['_requiredDirectToolNames']
        assert not task['_requiredDirectToolNames']
        assert not task['_executable_tool_catalog']
    _disable_tools_for_turn(task)
    assert not task['_requiredDirectToolNames'] and not task['_executable_tool_catalog']

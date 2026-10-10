"""Frozen-input command parameters follow the current execution context."""
import copy
import pytest
from lib.tools.registry import ToolContext, assemble_tool_list
from lib.tools.project import PROJECT_TOOL_RUN_COMMAND

pytestmark = pytest.mark.unit


def command_schema(*, project_ready, evolution):
    ctx = ToolContext(
        cfg={'evolution': evolution, 'tools': {'nativeExposure': 'full'}},
        task_id='input-scope-test', project_path='/tmp/project' if project_ready else '',
        project_enabled=project_ready, code_exec_enabled=not project_ready,
        search_mode='off', search_enabled=False, fetch_enabled=False,
        browser_enabled=False, desktop_enabled=False, image_gen_enabled=False,
        human_guidance_enabled=False, scheduler_enabled=False, messages=[])
    tools, _ = assemble_tool_list(ctx)
    return next(tool for tool in tools if tool['function']['name'] == 'run_command')


@pytest.mark.parametrize('project_ready', [True, False])
@pytest.mark.parametrize('evolution', [
    {}, {'run_id': 'run', 'stage': 'reviewing'},
    {'run_id': 'run', 'stage': 'coordinator'}, {'stage': 'executing'}])
def test_ordinary_commands_do_not_advertise_goal_only_selection(project_ready, evolution):
    schema = command_schema(project_ready=project_ready, evolution=evolution)
    properties = schema['function']['parameters']['properties']
    assert 'command' in properties
    assert 'frozen_input_paths' not in properties


def test_frozen_selection_exposure_is_request_local():
    original = copy.deepcopy(PROJECT_TOOL_RUN_COMMAND)
    binding = {'run_id': 'run', 'stage': 'executing'}
    executor = command_schema(project_ready=True, evolution=binding)
    ordinary = command_schema(project_ready=True, evolution={})
    standalone = command_schema(project_ready=False, evolution=binding)
    executor_again = command_schema(project_ready=True, evolution=binding)
    assert 'frozen_input_paths' in executor['function']['parameters']['properties']
    assert 'frozen_input_paths' not in ordinary['function']['parameters']['properties']
    assert 'frozen_input_paths' not in standalone['function']['parameters']['properties']
    assert executor_again == executor
    assert PROJECT_TOOL_RUN_COMMAND == original

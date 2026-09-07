"""Launch specification for Flow-backed chat execution."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Event

import pytest

from lib.orchestration._builtin_definitions import (
    build_autopilot_definition,
)
from lib.orchestration_chat_launch import (
    OrchestrationChatHistoryPorts,
    OrchestrationChatToolAssemblyPorts,
    build_flow_initial_context,
    build_orchestration_chat_flow_launch,
    build_tools_for_chat_task,
)
from tests.support.orchestration_definitions import (
    build_verifier_loop_definition,
)


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_launch_input_ports_are_explicit_and_immutable():
    history_calls = []
    history = OrchestrationChatHistoryPorts(
        format_messages=lambda messages, **kwargs: history_calls.append(
            (messages, kwargs)) or 'bounded history',
    )
    tool_calls = []
    tools = OrchestrationChatToolAssemblyPorts(
        resolve_model_config=lambda config, task_id: {
            'project_path': '/repo', 'project_enabled': True,
            'search_mode': 'off', 'search_enabled': False,
            'fetch_enabled': False, 'code_exec_enabled': False,
            'browser_enabled': False, 'desktop_enabled': False,
            'image_gen_enabled': False,
            'human_guidance_enabled': False, 'scheduler_enabled': False,
            'model': 'model-x',
        },
        assemble_tools=lambda *args, **kwargs: tool_calls.append(
            (args, kwargs)) or ([{'name': 'tool'}], True),
    )
    task = {
        'id': 'task-ports', 'convId': 'conv-ports', 'config': {},
        'messages': [{'role': 'user', 'content': 'ship it'}],
    }

    assert build_flow_initial_context(task, ports=history) == 'bounded history'
    assert build_tools_for_chat_task(task, ports=tools) == (
        [{'name': 'tool'}], 'model-x', '/repo')
    assert history_calls[0][1] == {'char_budget': 48_000}
    assert tool_calls[0][1]['conv_id'] == 'conv-ports'
    with pytest.raises(FrozenInstanceError):
        history.format_messages = lambda *_args: ''  # type: ignore[misc]


def test_launch_input_cores_only_consume_ports():
    source = (ROOT / 'lib' / 'orchestration_chat_launch.py').read_text()
    history_core = source[source.index(
        'def build_flow_initial_context('):source.index(
        '\n\ndef build_tools_for_chat_task')]
    tool_core = source[source.index(
        'def build_tools_for_chat_task('):source.index(
        '\n\n@dataclass(frozen=True)\nclass OrchestrationChatFlowLaunchSpec')]

    assert 'ports.format_messages(' in history_core
    assert 'ports.resolve_model_config(' in tool_core
    assert 'ports.assemble_tools(' in tool_core
    assert 'from lib.tasks_pkg' not in history_core + tool_core


@pytest.mark.parametrize(
    ('definition', 'projection', 'phase'),
    [
        (build_verifier_loop_definition(), 'critic', 'planning'),
        (build_autopilot_definition(), 'autopilot', 'working'),
    ],
)
def test_launch_resolves_projection_phase_and_detached_execution_options(
    definition, projection, phase,
):
    tools = [{'function': {'name': 'write_file'}}]
    abort = Event()
    task = {
        'id': 'task-one',
        'abort_event': abort,
        'config': {'thinkingEnabled': False},
    }
    launch = build_orchestration_chat_flow_launch(
        task,
        definition,
        max_iterations=7,
        tool_builder=lambda _task: (tools, 'model-x', '/repo'),
        context_builder=lambda _task: 'history',
        system_prompt_builder=lambda _task: 'policy',
    )
    tools.append({'function': {'name': 'late'}})

    assert launch.projection == projection
    assert launch.initial_phase == phase
    assert launch.initial_context == 'history'
    assert launch.system_prompt_base == 'policy'
    assert len(launch.tools) == 1
    assert launch.thinking_enabled is False
    first = launch.execution_kwargs(
        task, subflow_resolver=lambda _ref: {'nodes': []})
    second = launch.execution_kwargs(
        task, subflow_resolver=lambda _ref: None)
    assert first is not second
    assert first['initial_context'] == 'history'
    assert first['abort_check'].__self__ is abort
    assert first['abort_check']() is False
    assert first['executor_options']['max_iterations'] == 7
    assert first['executor_options']['all_tools'] is not tools
    assert first['executor_options']['model'] == 'model-x'
    assert first['executor_options']['model_routing_policy'] == (
        'selected' if projection == 'autopilot' else 'role_tier')
    assert first['executor_options']['project_path'] == '/repo'
    assert first['executor_options']['system_prompt_base'] == 'policy'
    assert first['executor_options']['thinking_enabled'] is False


def test_task_projection_and_default_thinking_policy_have_one_owner():
    task = {'id': 'task-two', 'config': {}}
    launch = build_orchestration_chat_flow_launch(
        task,
        build_autopilot_definition(),
        max_iterations=0,
        tool_builder=lambda _task: ([], '', ''),
        context_builder=lambda _task: '',
        system_prompt_builder=lambda _task: '',
    )

    launch.apply_task_projection(task, label='autopilot')

    assert launch.max_iterations == 1
    assert launch.thinking_enabled is True
    assert task == {
        'id': 'task-two',
        'config': {},
        'flow_mode': True,
        '_flow_projection': 'autopilot',
        '_flow_phase': 'working',
        '_flow_iteration': 0,
        '_flow_label': 'autopilot',
    }


def test_flow_runner_only_assembles_the_launch_spec():
    runner = (
        ROOT / 'lib' / 'orchestration_chat_flow_runner.py').read_text()
    runtime = (
        ROOT / 'lib' / 'orchestration_chat_flow_runtime.py').read_text()

    assert 'execute_orchestration_chat_flow_task(' in runner
    assert 'build_orchestration_chat_flow_launch(' in runtime
    assert 'launch.apply_task_projection(task, label=label)' in runtime
    assert '**launch.execution_kwargs(' in runtime
    assert 'initial_phase_for_flow' not in runner + runtime
    assert 'chat_projection_for_flow' not in runtime
    assert "executor_options={" not in runner + runtime


def test_task_starter_uses_provisional_flow_state_without_resolving_twice():
    starter = (ROOT / 'lib' / 'conversation_sync' / 'task_start.py').read_text()

    assert 'resolve_chat_flow_definition' not in starter
    assert 'initial_phase_for_flow' not in starter
    assert 'chat_projection_for_flow' not in starter
    assert "task['_flow_phase'] = 'working'" in starter
    assert "task['_flow_projection'] = 'flow'" in starter

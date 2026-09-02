"""Build the immutable launch specification for Flow-backed chat runs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from lib.log import get_logger
from lib.orchestration._chat_projection import chat_projection_for_flow
from lib.orchestration._execution_projection import initial_phase_for_flow


logger = get_logger(__name__)


@dataclass(frozen=True)
class OrchestrationChatHistoryPorts:
    """Canonical conversation-history renderer used by Flow chat launches."""

    format_messages: Callable

    @classmethod
    def defaults(cls) -> 'OrchestrationChatHistoryPorts':
        from lib.tasks_pkg.compaction._layer2._prompt import (
            _format_messages_for_summary,
        )

        return cls(format_messages=_format_messages_for_summary)


@dataclass(frozen=True)
class OrchestrationChatToolAssemblyPorts:
    """Model/tool policy dependencies required by Flow leaf agents."""

    resolve_model_config: Callable
    assemble_tools: Callable

    @classmethod
    def defaults(cls) -> 'OrchestrationChatToolAssemblyPorts':
        from lib.tasks_pkg.model_config import (
            _assemble_tool_list,
            _resolve_model_config,
        )

        return cls(
            resolve_model_config=_resolve_model_config,
            assemble_tools=_assemble_tool_list,
        )


def extract_user_request(task: dict) -> str:
    """Return the latest user text, including multimodal text blocks."""
    for message in reversed(task.get('messages') or []):
        if not isinstance(message, dict) or message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get('text', '')
                for block in content
                if isinstance(block, dict) and block.get('type') == 'text'
            ]
            return '\n'.join(part for part in parts if part)
    return ''


def extract_system_prompt(task: dict) -> str:
    """Return resolved system text on its dedicated Runner policy channel."""
    blocks: list[str] = []
    for message in task.get('messages') or []:
        if not isinstance(message, dict) or message.get('role') != 'system':
            continue
        content = message.get('content')
        if isinstance(content, str) and content.strip():
            blocks.append(content)
        elif isinstance(content, list):
            joined = '\n'.join(
                part.get('text', '')
                for part in content
                if isinstance(part, dict) and part.get('type') == 'text'
            ).strip()
            if joined:
                blocks.append(joined)
    return '\n\n'.join(blocks)


def build_flow_initial_context(
    task: dict,
    *,
    ports: OrchestrationChatHistoryPorts | None = None,
) -> str:
    """Render bounded conversation history with latest-user fallback."""
    messages = task.get('messages') or []
    try:
        ports = ports or OrchestrationChatHistoryPorts.defaults()
        rendered = ports.format_messages(messages, char_budget=48_000)
        if rendered.strip():
            return rendered
    except Exception as exc:
        logger.debug(
            '[FlowChat] full-history render failed, using latest user: %s',
            exc,
        )
    return extract_user_request(task)


def build_tools_for_chat_task(
    task: dict,
    *,
    ports: OrchestrationChatToolAssemblyPorts | None = None,
) -> tuple[list, str, str]:
    """Reuse the canonical chat model/tool assembly for Flow leaf agents."""
    try:
        ports = ports or OrchestrationChatToolAssemblyPorts.defaults()
        config = task.get('config') or {}
        model_config = ports.resolve_model_config(config, task['id'])
        tools, _has_real = ports.assemble_tools(
            config,
            model_config['project_path'],
            model_config['project_enabled'],
            task['id'],
            model_config['search_mode'],
            model_config['search_enabled'],
            model_config['fetch_enabled'],
            model_config['code_exec_enabled'],
            model_config['browser_enabled'],
            model_config['desktop_enabled'],
            image_gen_enabled=model_config['image_gen_enabled'],
            human_guidance_enabled=model_config['human_guidance_enabled'],
            scheduler_enabled=model_config['scheduler_enabled'],
            messages=task.get('messages'),
            conv_id=task.get('convId', ''),
        )
        return (
            tools,
            str(model_config.get('model') or ''),
            str(model_config.get('project_path') or ''),
        )
    except Exception as exc:
        logger.error(
            '[FlowChat] tool assembly failed: %s', exc, exc_info=True)
        return [], '', ''


@dataclass(frozen=True)
class OrchestrationChatFlowLaunchSpec:
    """Detached launch facts shared by task, adapter and FlowExecutor."""

    projection: str
    initial_phase: str
    initial_context: str
    system_prompt_base: str
    tools: tuple[Any, ...]
    model: str
    project_path: str
    thinking_enabled: bool
    max_iterations: int

    def apply_task_projection(self, task: dict, *, label: str) -> None:
        # Canonical flow-task fields are shared by terminal projection,
        # fallback telemetry, and the live event sink.
        task.update({
            'flow_mode': True,
            '_flow_projection': self.projection,
            '_flow_phase': self.initial_phase,
            '_flow_iteration': 0,
            '_flow_label': label,
        })

    def execution_kwargs(
        self,
        task: dict,
        *,
        subflow_resolver: Callable[[str], dict | None],
    ) -> dict:
        abort_event = task.get('abort_event')
        return {
            'initial_context': self.initial_context,
            'abort_check': abort_event.is_set if abort_event else None,
            'executor_options': {
                'agent_runner': None,
                'max_iterations': self.max_iterations,
                'parent_task': task,
                'all_tools': list(self.tools),
                'model': self.model,
                'project_path': self.project_path,
                'system_prompt_base': self.system_prompt_base,
                'thinking_enabled': self.thinking_enabled,
                'subflow_resolver': subflow_resolver,
            },
        }


def build_orchestration_chat_flow_launch(
    task: dict,
    definition: dict,
    *,
    max_iterations: int,
    tool_builder: Callable[[dict], tuple[list, str, str]] | None = None,
    context_builder: Callable[[dict], str] | None = None,
    system_prompt_builder: Callable[[dict], str] | None = None,
) -> OrchestrationChatFlowLaunchSpec:
    """Resolve all pre-execution Chat Flow policy through one boundary."""
    tools, model, project_path = (tool_builder or build_tools_for_chat_task)(
        task)
    thinking = (task.get('config') or {}).get('thinkingEnabled')
    return OrchestrationChatFlowLaunchSpec(
        projection=chat_projection_for_flow(definition),
        initial_phase=initial_phase_for_flow(definition),
        initial_context=(context_builder or build_flow_initial_context)(task),
        system_prompt_base=(system_prompt_builder or extract_system_prompt)(task),
        tools=tuple(tools or ()),
        model=str(model or ''),
        project_path=str(project_path or ''),
        thinking_enabled=True if thinking is None else bool(thinking),
        max_iterations=max(1, int(max_iterations)),
    )


__all__ = [
    'OrchestrationChatHistoryPorts',
    'OrchestrationChatFlowLaunchSpec',
    'OrchestrationChatToolAssemblyPorts',
    'build_flow_initial_context',
    'build_orchestration_chat_flow_launch',
    'build_tools_for_chat_task',
    'extract_system_prompt',
    'extract_user_request',
]

"""Default SubAgent adapter for orchestration role nodes.

The graph interpreter depends only on the ``agent_runner(node, context,
iteration)`` protocol. This module is the production implementation of that
port: it maps one role node to the swarm substrate and maps live SubAgent
streaming back to engine-internal events.

Swarm imports remain lazy so importing/compiling the pure interpreter does not
load the agent runtime and tests can continue injecting a lightweight runner.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration_runner_result import OrchestrationAgentResult
from lib.orchestration_tool_usage import classify_orchestration_tool_usage


@dataclass(frozen=True)
class OrchestrationAgentRunnerConfig:
    """Explicit configuration passed from a FlowExecutor to its adapter."""

    parent_task: dict | None = None
    all_tools: list = field(default_factory=list)
    model: str = ''
    project_path: str = ''
    system_prompt_base: str = ''
    thinking_enabled: bool = True

    def executor_options(self) -> dict[str, Any]:
        """Return the public FlowExecutor options for an isolated child."""
        return {
            'parent_task': self.parent_task,
            'all_tools': self.all_tools,
            'model': self.model,
            'project_path': self.project_path,
            'system_prompt_base': self.system_prompt_base,
            'thinking_enabled': self.thinking_enabled,
        }


class OrchestrationSubAgentRunner:
    """Blocking callable that executes one orchestration role as a SubAgent."""

    def __init__(
        self,
        config: OrchestrationAgentRunnerConfig,
        *,
        emit: Callable[[dict], None],
        abort_check: Callable[[], bool],
    ):
        self._config = config
        self._emit = emit
        self._abort_check = abort_check

    def __call__(
        self, node: dict, context: str, iteration: int,
    ) -> OrchestrationAgentResult:
        """Run one role node and normalize the SubAgent result contract."""
        del iteration  # reserved by the shared runner protocol

        from lib.swarm.agent import SubAgent
        from lib.swarm.protocol import SubAgentStatus, SubTaskSpec

        spec = SubTaskSpec(
            role=node.get('role', 'general'),
            objective=(render_role_brief(node) or node.get('name')
                       or 'Execute this step.'),
            context=context,
            model_tier=resolve_node_runtime_param(node, 'tier'),
        )
        parent = self._config.parent_task or {
            'id': 'flow',
            'convId': 'flow',
            'events_lock': threading.Lock(),
            'events': [],
            'toolRounds': [],
            'phase': 'tool',
            'config': {},
        }
        node_id = node.get('id')
        role = node.get('role', 'general')
        emits = resolve_node_runtime_param(node, 'emits')
        thinking_parts: list[str] = []

        def stream_sink(kind: str, chunk: str, *, phase: str = '', **meta):
            if kind == 'phase':
                self._emit({
                    'type': 'step_phase',
                    'node_id': node_id,
                    'role': role,
                    'emits': emits,
                    'phase': phase or 'working',
                    'detail': chunk,
                    **meta,
                })
                return
            if kind == 'thinking' and chunk:
                thinking_parts.append(chunk)
            self._emit({
                'type': 'step_delta',
                'node_id': node_id,
                'role': role,
                'emits': emits,
                'kind': kind,
                'chunk': chunk,
            })

        agent = SubAgent(
            spec,
            parent_task=parent,
            all_tools=self._config.all_tools,
            system_prompt_base=self._config.system_prompt_base,
            model=self._config.model,
            thinking_enabled=self._config.thinking_enabled,
            abort_check=self._abort_check,
            project_path=self._config.project_path,
            stream_sink=stream_sink,
        )
        result = agent.run()
        tool_log = result.tool_log or []
        return OrchestrationAgentResult(
            output=result.final_answer or '',
            status=result.status,
            error=(
                result.error_message
                if result.status != SubAgentStatus.COMPLETED.value else ''
            ),
            thinking=''.join(thinking_parts),
            tool_usage=classify_orchestration_tool_usage({
                'tool_log': tool_log,
            }),
        )


__all__ = [
    'OrchestrationAgentRunnerConfig',
    'OrchestrationSubAgentRunner',
]

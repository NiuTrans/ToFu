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
from lib.orchestration_runner_result import (
    OrchestrationAgentResult,
    OrchestrationModelRoute,
)
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
    model_routing_policy: str = 'role_tier'

    def __post_init__(self) -> None:
        if self.model_routing_policy not in {'selected', 'role_tier'}:
            raise ValueError(
                f'unsupported orchestration model routing policy: '
                f'{self.model_routing_policy!r}')
        if self.model_routing_policy == 'selected' and not self.model:
            raise ValueError(
                'selected-model orchestration requires a model')

    def executor_options(self) -> dict[str, Any]:
        """Return the public FlowExecutor options for an isolated child."""
        return {
            'parent_task': self.parent_task,
            'all_tools': self.all_tools,
            'model': self.model,
            'project_path': self.project_path,
            'system_prompt_base': self.system_prompt_base,
            'thinking_enabled': self.thinking_enabled,
            'model_routing_policy': self.model_routing_policy,
        }


class OrchestrationSubAgentRunner:
    """Blocking callable that executes one orchestration role as a SubAgent."""

    def __init__(
        self,
        config: OrchestrationAgentRunnerConfig,
        *,
        emit: Callable[[dict], None],
        abort_check: Callable[[], bool],
        model_resolver: Callable[..., str] | None = None,
    ):
        self._config = config
        self._emit = emit
        self._abort_check = abort_check
        self._model_resolver = model_resolver

    def _resolve_model_route(self, node: dict) -> OrchestrationModelRoute:
        """Resolve the leaf route once, before constructing the SubAgent."""
        selected_model = str(self._config.model or '')
        role = str(node.get('role') or 'general')
        tier = str(resolve_node_runtime_param(node, 'tier') or 'standard')
        routing_policy = self._config.model_routing_policy
        if routing_policy == 'selected':
            resolved_model = selected_model
        else:
            resolver = self._model_resolver
            if resolver is None:
                from lib.swarm.registry import resolve_model_for_tier
                resolver = resolve_model_for_tier
            parent_config = (
                (self._config.parent_task or {}).get('config') or {})
            resolver_kwargs = {
                'role': role,
                'provider_id': str(
                    parent_config.get('_pinned_provider_id') or ''),
            }
            parent_task = self._config.parent_task or {}
            if parent_task.get('_userId') is not None:
                from lib.tasks_pkg.manager import task_user_id
                resolver_kwargs.update({
                    'owner_user_id': task_user_id(parent_task),
                    'tenant_id': parent_task.get('_tenant_id'),
                })
            resolved_model = str(resolver(
                tier, selected_model, **resolver_kwargs) or selected_model)
        return OrchestrationModelRoute(
            selected_model=selected_model,
            resolved_model=resolved_model,
            role=role,
            tier=tier,
            kind=('selected' if routing_policy == 'selected'
                  else 'role_tier'),
        )

    def __call__(
        self, node: dict, context: str, iteration: int,
    ) -> OrchestrationAgentResult:
        """Run one role node and normalize the SubAgent result contract."""
        del iteration  # reserved by the shared runner protocol

        from lib.swarm.agent import SubAgent
        from lib.swarm.protocol import SubAgentStatus, SubTaskSpec

        model_route = self._resolve_model_route(node)
        spec = SubTaskSpec(
            role=node.get('role', 'general'),
            objective=(render_role_brief(node) or node.get('name')
                       or 'Execute this step.'),
            context=context,
            model_override=model_route.resolved_model,
            model_tier=model_route.tier,
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
                    'modelRoute': model_route.to_projection(),
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

        def tool_event_sink(event: dict) -> None:
            self._emit({
                'type': 'step_tool_event',
                'node_id': node_id,
                'role': role,
                'emits': emits,
                'event': dict(event),
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
            tool_event_sink=tool_event_sink,
        )
        actual_model = str(
            getattr(agent, 'model', '') or model_route.resolved_model)
        if actual_model != model_route.resolved_model:
            model_route = OrchestrationModelRoute(
                selected_model=model_route.selected_model,
                resolved_model=actual_model,
                role=model_route.role,
                tier=model_route.tier,
                kind=model_route.kind,
            )
        if model_route.switched:
            self._emit({
                'type': 'step_phase',
                'node_id': node_id,
                'role': role,
                'emits': emits,
                'phase': 'working',
                'detail': (
                    f'Model routing: {model_route.selected_model} → '
                    f'{model_route.resolved_model} '
                    f'({model_route.role}, {model_route.tier})'
                ),
                'detailKey': 'stream.phase.modelRouted',
                'detailArgs': {
                    'from': model_route.selected_model,
                    'to': model_route.resolved_model,
                    'role': model_route.role,
                    'tier': model_route.tier,
                },
                'model': model_route.resolved_model,
                'modelRoute': model_route.to_projection(),
            })
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
            model_route=model_route,
            tool_usage=classify_orchestration_tool_usage({
                'tool_log': tool_log,
            }),
            tool_log=tuple(
                dict(row) for row in tool_log if isinstance(row, dict)),
        )


__all__ = [
    'OrchestrationAgentRunnerConfig',
    'OrchestrationSubAgentRunner',
]

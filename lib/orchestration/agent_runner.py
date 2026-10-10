"""Graph scheduling/presentation policy over the ordinary invocation owner.

Role nodes select context, capabilities and model once. They do not own model
loops, checkpoints, tool proxies, or completion reconstruction. Runtime imports
stay lazy so the pure graph interpreter can use deterministic runner fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration.runner_result import (
    OrchestrationAgentResult,
    OrchestrationModelRoute,
)
from lib.orchestration.tool_usage import classify_orchestration_tool_usage
from lib.orchestration.usage_accounting import project_child_accounting


@dataclass(frozen=True)
class OrchestrationAgentRunnerConfig:
    """Explicit configuration passed from a FlowExecutor to its adapter."""

    parent_task: dict | None = None
    all_tools: list = field(default_factory=list)
    model: str = ''
    project_path: str = ''
    system_prompt_base: str = ''
    thinking_enabled: bool | None = None
    model_routing_policy: str = 'role_tier'
    worker_context_policy: str = 'handoff'

    def __post_init__(self) -> None:
        if self.worker_context_policy not in {'handoff', 'conversation'}:
            raise ValueError(f'unsupported worker context policy: {self.worker_context_policy!r}')
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
            'worker_context_policy': self.worker_context_policy,
        }


class OrchestrationInvocationRunner:
    """Blocking graph leaf using the same preparation-to-settlement as chat."""

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
        from lib.goal_runs.worker_context import GoalWorkerContext
        self._worker_context = GoalWorkerContext()

    def clear_worker_context(self) -> None:
        """Release the single working set on success, cancellation or error."""
        self._worker_context.clear()

    def snapshot_worker_context(self) -> dict:
        return self._worker_context.snapshot()

    def restore_worker_context(self, snapshot: dict) -> None:
        self._worker_context.restore(snapshot)

    def _resolve_model_route(self, node: dict) -> OrchestrationModelRoute:
        """Resolve the leaf route once, before constructing the invocation."""
        selected_model = str(self._config.model or '')
        role = str(node.get('role') or 'general')
        tier = str(resolve_node_runtime_param(node, 'tier') or 'standard')
        routing_policy = self._config.model_routing_policy
        override = resolve_node_runtime_param(node, 'model_override')
        if override is not None and (not isinstance(override, str) or len(override) > 256):
            raise ValueError('node model_override must be a bounded model id')
        override = (override or '').strip()
        if routing_policy == 'selected':
            if override and override != selected_model:
                raise ValueError('node model override conflicts with selected-model authority')
            resolved_model = selected_model
        elif override:
            resolved_model = override
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
            kind=('selected' if routing_policy == 'selected' or override
                  else 'role_tier'),
        )

    def __call__(
        self, node: dict, context: str, iteration: int, *, before_dispatch=None,
    ) -> OrchestrationAgentResult:
        """Schedule an owner-scoped invocation; project canonical events only."""
        from lib.tasks_pkg.child_invocation import create_child_invocation, run_child_invocation
        from lib.swarm.registry import scope_tools_for_role, get_role_system_suffix
        from lib.turn_verdict import derive_task_verdict

        parent = self._config.parent_task or {}
        from lib.tasks_pkg.manager import task_user_id
        task_user_id(parent)
        if not parent.get('id'):
            raise ValueError('Graph invocation requires a parent task')
        model_route = self._resolve_model_route(node)
        node_id = node.get('id')
        role = node.get('role', 'general')
        emits = resolve_node_runtime_param(node, 'emits')
        from lib.goal_runs.worker_context import is_conversation_worker
        conversation_worker = is_conversation_worker(
            node, self._config.worker_context_policy)

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

        def event_sink(event: dict) -> None:

            from lib.agent_core.publication import check_publication
            check_publication()
            kind = event.get('type')

            if kind in {'delta_reset', 'retry_reset', 'round_end', 'done'}:
                # These boundaries run after provider ingress has drained.
                # The exact same assembly as a normal checkpoint owns order;
                # the graph cannot recreate it later from display previews.
                from lib.tasks_pkg.invocation import invocation_output_projection
                self._emit({'type': 'step_event', 'node_id': node_id,
                            'role': role, 'emits': emits,
                            'event': {'type': 'state',
                                      **invocation_output_projection(child)}})
            if kind in {'round_start', 'round_end', 'delta_reset', 'retry_reset'}:
                self._emit({'type': 'step_event', 'node_id': node_id,
                            'role': role, 'emits': emits, 'event': dict(event)})
                return
            if kind == 'delta':
                for field in ('thinking', 'content'):
                    if event.get(field):
                        stream_sink(field, event[field])
            elif kind == 'phase':
                stream_sink('phase', event.get('detail') or '',
                            phase=event.get('phase') or 'working',
                            **{key: event[key] for key in ('detailKey', 'detailArgs', 'attempt')
                               if key in event})
            elif kind in {'tool_start', 'tool_progress', 'tool_result',
                          'tool_complete', 'tool_compacted'}:
                tool_event_sink(event)

        from lib.swarm.routing import read_only_swarm_tools, tool_schema_name
        tools = ([tool for tool in self._config.all_tools
                  if tool_schema_name(tool) != 'ask_human'] if conversation_worker
                 else scope_tools_for_role(role, self._config.all_tools))
        if not conversation_worker and str(((parent.get('config') or {}).get('_toolOrchestration') or {}).get(
                'multiAgent') or '').lower() == 'read_only':
            tools = read_only_swarm_tools(parent, tools)
        allowed = resolve_node_runtime_param(node, 'tools')
        if allowed is not None:
            if (not isinstance(allowed, list) or len(allowed) > 128
                    or any(not isinstance(name, str) or not name.strip()
                           or len(name) > 256 for name in allowed)):
                raise ValueError('node tools must be a bounded list of tool names')
            allowed_names = frozenset(allowed)
            tools = [tool for tool in tools if tool_schema_name(tool) in allowed_names]
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
        child = create_child_invocation(
            parent, model=model_route.resolved_model, tools=tools,
            objective=render_role_brief(node) or node.get('name') or 'Execute this step.',
            context=context, role_policy=get_role_system_suffix(role),
            conversation_messages=(self._worker_context.prepare(parent, directive=context)
                                   if conversation_worker else None),
            system_prompt=self._config.system_prompt_base,
            project_path=self._config.project_path,
            thinking_enabled=self._config.thinking_enabled,
            event_sink=event_sink, abort_check=lambda: self._abort_check() or (
                callable(node.get('_runtimeAbortCheck')) and node['_runtimeAbortCheck']()))
        child['_flow_iteration'] = iteration
        child['_flow_phase'] = role
        if conversation_worker:
            self._worker_context.hydrate(
                child,
                objective=render_role_brief(node),
                # The run's authoritative objective, stamped from the accepted
                # human turn at the command boundary and the GoalRun start.
                # Re-stamped verbatim every iteration so compaction can never
                # drift it (Hole A) and the prefix cache stays warm.
                goal_objective=str(parent.get('_goalObjective') or ''),
            )
        if before_dispatch is None:
            run_child_invocation(child)
        else:
            run_child_invocation(child, before_dispatch=before_dispatch)
        verdict = derive_task_verdict(child)
        if conversation_worker and (child.get('_messages_include_current_run')
                                    or verdict.status.value == 'completed'):
            self._worker_context.accept(child)
            if parent.get('convId') and parent.get('_attemptId'):
                from copy import deepcopy
                parent['_goalWorkerCheckpoint'] = {
                    'messages': self._worker_context.messages,
                    'task_state': deepcopy(self._worker_context.task_state),
                }
        elif role == 'virtual_user' and verdict.status.value == 'completed':
            self._worker_context.review(child.get('content') or '')
        projection = {key: child[key] for key in (
            'content', 'thinking', 'segments', 'toolRounds', 'finishReason',
            'streamState', 'error', 'model', 'modifiedFiles', 'modifiedFileList',
        ) if child.get(key) is not None}
        projection.update(project_child_accounting(
            child, invocation_id=str(child.get('id') or '')))
        return OrchestrationAgentResult(
            output=child.get('content') or '',
            status={'completed': 'completed', 'interrupted': 'cancelled',
                    'truncated': 'stalled'}.get(verdict.status.value, 'failed'),
            error=str(child.get('error') or ''),
            thinking=child.get('thinking') or '',
            model_route=model_route,
            tool_usage=classify_orchestration_tool_usage({
                'tool_log': child.get('toolRounds') or [],
            }),
            output_projection=projection,
        )


# Import compatibility only; there is no legacy execution branch.
OrchestrationSubAgentRunner = OrchestrationInvocationRunner

__all__ = ['OrchestrationAgentRunnerConfig', 'OrchestrationInvocationRunner',
           'OrchestrationSubAgentRunner']

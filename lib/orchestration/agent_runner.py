"""Graph scheduling/presentation policy over the ordinary invocation owner.

Role nodes select context, capabilities and model once. They do not own model
loops, checkpoints, tool proxies, or completion reconstruction. Runtime imports
stay lazy so the pure graph interpreter can use deterministic runner fixtures.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, replace
import threading
from typing import Any

from lib.orchestration._execution_projection import render_role_brief
from lib.orchestration._runtime_params import resolve_node_runtime_param
from lib.orchestration.runner_result import (
    OrchestrationAgentResult,
    OrchestrationModelRoute,
)
from lib.orchestration.tool_usage import classify_orchestration_tool_usage
from lib.orchestration.usage_accounting import project_child_accounting

from lib.log import get_logger

logger = get_logger(__name__)


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


@dataclass(frozen=True)
class PreparedOrchestrationInvocation:
    """Borrowed child carrier and one ordinary execution entry; caller owns scope."""
    child: dict
    run: Callable[..., OrchestrationAgentResult]
    prepare_completion: Callable[..., dict | None]


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
                from lib.model_routing.domain import NativeModelSelection
                native_selection=parent_task.get('_model_routing_selection')
                if isinstance(native_selection,NativeModelSelection):
                    from lib.identity import PrincipalContext
                    resolver_kwargs['provider_id']=native_selection.preferred_provider_id or (
                        native_selection.provider_offering.provider_id if native_selection.provider_offering else '')
                    resolver_kwargs['tenant_id']=PrincipalContext.from_payload(
                        parent_task['_principalContext']).tenant_id

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

    def __call__(self, node: dict, context: str, iteration: int, *, before_dispatch=None):
        with self.prepare(node, context, iteration, durable_admission=before_dispatch is not None) as prepared:
            return prepared.run(before_dispatch=before_dispatch)

    @contextmanager
    def prepare(self, node: dict, context: str, iteration: int, *, durable_admission=False,
                transfer_context=False):
        """Prepare through the ordinary child owner; scope exit rejects unused work."""
        from lib.tasks_pkg.child_invocation import (
            create_child_invocation, run_child_invocation, close_unstarted_child_invocation)
        from lib.swarm.registry import scope_tools_for_role, get_role_system_suffix

        parent = self._config.parent_task or {}
        original_context = self.snapshot_worker_context() if transfer_context else None
        transfer_config = (replace(self._config, all_tools=deepcopy(self._config.all_tools))
                           if transfer_context else None)
        execution_owner = self
        runtime_abort = node.get('_runtimeAbortCheck')
        child = None
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
                execution_owner._emit({
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
            execution_owner._emit({
                'type': 'step_delta',
                'node_id': node_id,
                'role': role,
                'emits': emits,
                'kind': kind,
                'chunk': chunk,
            })

        def tool_event_sink(event: dict) -> None:
            execution_owner._emit({
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

            if child is not None and kind in {'delta_reset', 'retry_reset', 'round_end', 'done'}:
                # These boundaries run after provider ingress has drained.
                # The exact same assembly as a normal checkpoint owns order;
                # the graph cannot recreate it later from display previews.
                from lib.tasks_pkg.invocation import invocation_output_projection
                execution_owner._emit({'type': 'step_event', 'node_id': node_id,
                            'role': role, 'emits': emits,
                            'event': {'type': 'state',
                                      **invocation_output_projection(child)}})
            if kind in {'round_start', 'round_end', 'delta_reset', 'retry_reset'}:
                execution_owner._emit({'type': 'step_event', 'node_id': node_id,
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
        if durable_admission:
            from lib.tasks_pkg.invocation_registration import storage_free_parent
            if not storage_free_parent(parent):
                # Even an unbounded registered tree needs a complete observation
                # account before its first child attaches and emits a receipt.
                from lib.agent_core.execution_budget import _ensure_execution_budget
                _ensure_execution_budget(parent)
        child = create_child_invocation(
            parent, model=model_route.resolved_model, tools=tools,
            objective=render_role_brief(node) or node.get('name') or 'Execute this step.',
            context=context, role_policy=get_role_system_suffix(role),
            conversation_messages=(self._worker_context.prepare(parent, directive=context)
                                   if conversation_worker else None),
            system_prompt=self._config.system_prompt_base,
            project_path=self._config.project_path,
            thinking_enabled=self._config.thinking_enabled,
            event_sink=event_sink, abort_check=lambda: execution_owner._abort_check() or (
                callable(runtime_abort) and runtime_abort()))
        entered = closed = executing = False
        execution_thread = None
        preparation_lock = threading.Condition()
        completion_writer = None

        def prepare_completion(event):
            nonlocal completion_writer
            from lib.tasks_pkg.invocation_registration import registered_invocation_id
            with preparation_lock:
                if entered or closed or not durable_admission or completion_writer is not None:
                    raise ValueError('Prepared child cannot reserve another completion binding')
                if registered_invocation_id(child) is None:
                    return None
                from lib.orchestration.invocation_recovery import RegisteredInvocationCompletion
                completion_writer = RegisteredInvocationCompletion(child)
                return completion_writer.prepare(event)

        def execute(*, before_dispatch=None, recipient_runner=None, runtime_abort_check=None):
            nonlocal entered, executing, execution_thread, execution_owner, runtime_abort
            with preparation_lock:
                if entered or closed or (durable_admission and not callable(before_dispatch)):
                    raise ValueError('Prepared graph child is closed, consumed or lacks durable admission')
                if recipient_runner is not None and recipient_runner is not self:
                    if (not transfer_context or not isinstance(recipient_runner, OrchestrationInvocationRunner)
                            or recipient_runner._config.parent_task is not parent
                            or recipient_runner._config != transfer_config
                            or recipient_runner.snapshot_worker_context() != original_context):
                        raise ValueError('Prepared graph child context or execution owner changed')
                    execution_owner = recipient_runner
                    runtime_abort = runtime_abort_check
                entered = executing = True
                execution_thread = threading.get_ident()
            try:
                if before_dispatch is None:
                    run_child_invocation(child)
                else:
                    def admit_registered_child():
                        from lib.tasks_pkg.invocation_registration import registered_invocation_id
                        invocation_id = registered_invocation_id(child)
                        if invocation_id is None:
                            before_dispatch()
                        else:
                            from lib.orchestration.invocation_recovery import RegisteredInvocationCompletion
                            before_dispatch(invocation_id=invocation_id,
                                            completion=completion_writer or RegisteredInvocationCompletion(child))
                    run_child_invocation(child, before_dispatch=admit_registered_child)
                return execution_owner._complete_invocation(child, conversation_worker=conversation_worker,
                    role=role, model_route=model_route)
            finally:
                with preparation_lock:
                    executing = False
                    preparation_lock.notify_all()

        try:
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
            yield PreparedOrchestrationInvocation(child, execute, prepare_completion)
        finally:
            import sys
            primary = sys.exception()
            interrupted = None
            with preparation_lock:
                closed = True
                if executing and execution_thread == threading.get_ident():
                    raise RuntimeError('Prepared child cannot close its own active scope')
                if executing:
                    child['abort_event'].set()
                while executing:
                    try:
                        preparation_lock.wait()
                    except BaseException as error:
                        logger.debug('prepare failure handled: %s', type(error).__name__)
                        interrupted = interrupted or error
                unused = not entered
            if interrupted is not None:
                if primary is None:
                    raise interrupted
                primary.add_note('Prepared child close was interrupted; its invocation still joined')
            if unused:
                try:
                    close_unstarted_child_invocation(child, reason='Prepared graph child was not dispatched')
                except BaseException as error:
                    if primary is None:
                        raise
                    primary.add_note('Unstarted graph child cleanup failed: '+type(error).__name__)


    def _complete_invocation(self, child, *, conversation_worker, role, model_route):
        """Project one ordinary child into this actual graph context owner."""
        from lib.turn_verdict import derive_task_verdict
        parent = self._config.parent_task or {}
        verdict = derive_task_verdict(child)
        if conversation_worker and (child.get('_messages_include_current_run')
                                    or verdict.status.value == 'completed'):
            self._worker_context.accept(child)
            if parent.get('convId') and parent.get('_attemptId'):
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
           'OrchestrationSubAgentRunner', 'PreparedOrchestrationInvocation']

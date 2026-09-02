"""Canonical FlowExecutor-backed chat task runtime.

This is the single assembly seam shared by goal mode (autopilot graph) and
selected Studio flows. It binds the engine to chat SSE, durable turn persistence and
terminal projection without owning entry selection or rollout flags.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lib.log import audit_log, get_logger
from lib.orchestration_chat_launch import (
    build_flow_initial_context,
    build_orchestration_chat_flow_launch,
    build_tools_for_chat_task,
    extract_system_prompt,
)


logger = get_logger(__name__)


@dataclass(frozen=True)
class OrchestrationChatFlowRuntimePorts:
    """All delivery side effects required by the chat-flow runtime."""

    append_event: Callable
    persist_task_result: Callable
    notify_terminal: Callable
    stamp_terminal: Callable
    store_turns: Callable
    sync_turns: Callable
    complete_autopilot: Callable

    @classmethod
    def defaults(cls) -> 'OrchestrationChatFlowRuntimePorts':
        """Resolve production task/persistence adapters at execution time."""
        from lib.tasks_pkg.manager import (
            append_event,
            notify_terminal_conversation_change,
            persist_task_result,
            stamp_chat_task_terminal,
        )
        from lib.orchestration_chat_autopilot import (
            complete_orchestration_autopilot_flow,
        )
        from lib.orchestration_chat_turn_sync import (
            store_flow_turns_on_task,
            sync_flow_turns_to_conversation,
        )

        return cls(
            append_event=append_event,
            persist_task_result=persist_task_result,
            notify_terminal=notify_terminal_conversation_change,
            stamp_terminal=stamp_chat_task_terminal,
            store_turns=store_flow_turns_on_task,
            sync_turns=sync_flow_turns_to_conversation,
            complete_autopilot=complete_orchestration_autopilot_flow,
        )


def execute_orchestration_chat_flow_task(
    task: dict,
    definition: dict,
    *,
    label: str,
    max_iterations: int,
    definition_service,
    ports: OrchestrationChatFlowRuntimePorts | None = None,
    tool_builder: Callable = build_tools_for_chat_task,
    context_builder: Callable = build_flow_initial_context,
    system_prompt_builder: Callable = extract_system_prompt,
) -> None:
    """Execute one definition through the canonical chat delivery boundary.

    The definition service is explicit because subflow resolution must use the
    same repository snapshot chosen by the entry adapter. Builder callbacks
    remain injectable for compatibility tests and alternate chat launchers.
    """
    from lib.orchestration_chat_completion import (
        OrchestrationChatFlowCompletion,
    )
    from lib.orchestration_chat_event_sink import (
        OrchestrationChatTaskEventSink,
    )
    from lib.orchestration_chat_turn_persistence import (
        OrchestrationChatTurnPersistence,
    )
    from lib.orchestration.runtime_service import execute_flow
    from lib.orchestration_chat_flow_adapter import FlowEventAdapter

    if 'id' not in task:
        raise ValueError(
            'orchestration chat flow called with a task missing an id')
    if definition_service is None:
        raise ValueError(
            'orchestration chat flow requires a definition service')
    ports = ports or OrchestrationChatFlowRuntimePorts.defaults()

    task_id = str(task['id'])
    launch = build_orchestration_chat_flow_launch(
        task,
        definition,
        max_iterations=max_iterations,
        tool_builder=tool_builder,
        context_builder=context_builder,
        system_prompt_builder=system_prompt_builder,
    )
    launch.apply_task_projection(task, label=label)
    projection = launch.projection

    audit_log('flow_via_chat_start', task_id=task_id, flow=label)
    logger.info(
        '[FlowChat] task=%s START label=%s (FlowExecutor path)',
        task_id[:8], label,
    )

    task_event_sink = OrchestrationChatTaskEventSink(
        task, ports.append_event)
    turn_persistence = OrchestrationChatTurnPersistence(
        task,
        store_turns=ports.store_turns,
        sync_turns=ports.sync_turns,
    )

    virtual_user_flow = projection == 'autopilot'
    adapter = FlowEventAdapter(
        emit=turn_persistence,
        on_stream=task_event_sink,
        vu_flow=virtual_user_flow,
        vu_run_id=(task_id if virtual_user_flow else ''),
        projection=projection,
    )
    turn_persistence.bind(adapter.messages)

    outcome = execute_flow(
        definition,
        on_event=adapter.on_event,
        **launch.execution_kwargs(
            task,
            subflow_resolver=definition_service.get_definition,
        ),
    )
    completion = OrchestrationChatFlowCompletion(
        task,
        projection=projection,
        outcome=outcome,
        messages=adapter.messages,
        task_event_sink=task_event_sink,
        turn_persistence=turn_persistence,
        append_event=ports.append_event,
        persist_task_result=ports.persist_task_result,
        notify_terminal=ports.notify_terminal,
        stamp_terminal=ports.stamp_terminal,
    )
    terminal = completion.prepare()
    if virtual_user_flow:
        ports.complete_autopilot(task, terminal)
    completion.finish()

    audit_log(
        'flow_via_chat_complete',
        task_id=task_id,
        flow=label,
        status=terminal.chat_status,
        reason=terminal.stop_reason,
        iterations=completion.iterations,
    )
    logger.info(
        '[FlowChat] task=%s label=%s DONE status=%s reason=%s',
        task_id[:8], label, terminal.chat_status, terminal.stop_reason,
    )


__all__ = [
    'OrchestrationChatFlowRuntimePorts',
    'execute_orchestration_chat_flow_task',
]

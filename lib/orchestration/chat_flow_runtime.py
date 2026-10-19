"""Canonical FlowExecutor-backed chat task runtime.

This is the single assembly seam shared by goal mode (autopilot graph) and
selected Studio flows. It binds the engine to chat SSE, durable turn persistence and
terminal projection without owning entry selection or rollout flags.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lib.log import audit_log, get_logger
from lib.orchestration.chat_launch import (
    build_flow_initial_context,
    build_orchestration_chat_flow_launch,
    build_tools_for_chat_task,
    extract_system_prompt,
)


logger = get_logger(__name__)


# Bound the EXCERPT persisted into conversation settings (one record per run,
# re-serialized on every settings write) — the full stop report is persisted
# with the durable GoalRun transition event and fetched on demand instead, so
# the settings blob stays small while the report itself is never truncated.
GOAL_STOP_REPORT_EXCERPT_CHARS = 2000

# Hygiene ceiling for the full report persisted on the transition event.  It
# is deliberately far beyond any real VU verdict — the report content is
# never truncated in the product sense; this only bounds a pathological
# model output before it enters durable storage.
GOAL_STOP_REPORT_FULL_MAX_CHARS = 200_000

# Backwards-compatible alias for the excerpt bound.
GOAL_STOP_REPORT_MAX_CHARS = GOAL_STOP_REPORT_EXCERPT_CHARS


def _vu_stop_report(messages: list[dict]) -> str:
    """Source the concluded run's fold report from its own terminal evidence.

    The last virtual-user utterance IS the run's explanation of why it ended:
    the sign-off justification on a clean TASK_DONE, or the last unmet gap on
    a safety-cap cutoff (stuck / no_progress / max_iterations). Machine
    control tokens (the DONE sentinel, the [PROGRESS:] receipt) are stripped
    so the fold renders prose, not protocol.  The FULL text is returned — the
    settings-bound excerpt is cut by the caller.
    """
    from lib.agent_verdict import strip_machine_tokens
    for message in reversed(messages or []):
        if not isinstance(message, dict):
            continue
        if message.get('role') != 'user':
            continue
        content = str(message.get('content') or '')
        if content.strip():
            return strip_machine_tokens(content).strip()[
                :GOAL_STOP_REPORT_FULL_MAX_CHARS]
    return ''


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
    retry_turn_commit: Callable | None = None
    create_execution_journal: Callable | None = None

    @classmethod
    def defaults(cls) -> 'OrchestrationChatFlowRuntimePorts':
        """Resolve production task/persistence adapters at execution time."""
        from lib.tasks_pkg.manager import (
            append_event,
            notify_terminal_conversation_change,
            persist_task_result,
            stamp_chat_task_terminal,
        )
        from lib.orchestration.chat_autopilot import (
            complete_orchestration_autopilot_flow,
        )
        from lib.orchestration.chat_turn_sync import (
            store_flow_turns_on_task,
            sync_flow_turns_to_conversation,
        )

        from lib.turn_lifecycle import retry_turn_commit
        from lib.goal_runs.execution_journal import create_goal_execution_journal

        return cls(
            append_event=append_event,
            persist_task_result=persist_task_result,
            notify_terminal=notify_terminal_conversation_change,
            stamp_terminal=stamp_chat_task_terminal,
            store_turns=store_flow_turns_on_task,
            sync_turns=sync_flow_turns_to_conversation,
            complete_autopilot=complete_orchestration_autopilot_flow,
            retry_turn_commit=retry_turn_commit,
            create_execution_journal=create_goal_execution_journal,
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
    from lib.orchestration.chat_completion import (
        OrchestrationChatFlowCompletion,
    )
    from lib.orchestration.chat_event_sink import (
        OrchestrationChatTaskEventSink,
    )
    from lib.orchestration.chat_turn_persistence import (
        OrchestrationChatTurnPersistence,
    )
    from lib.orchestration.runtime_service import execute_flow
    from lib.orchestration.chat_flow_adapter import FlowEventAdapter

    if 'id' not in task:
        raise ValueError(
            'orchestration chat flow called with a task missing an id')
    if definition_service is None:
        raise ValueError(
            'orchestration chat flow requires a definition service')
    ports = ports or OrchestrationChatFlowRuntimePorts.defaults()

    task_id = str(task['id'])
    recovery_seed = task.get('_goalRecoverySeed')
    launch = recovery_seed.launch() if recovery_seed is not None else build_orchestration_chat_flow_launch(
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
        retry_commit=ports.retry_turn_commit,
    )

    virtual_user_flow = projection == 'autopilot'
    adapter = FlowEventAdapter(
        emit=turn_persistence,
        on_stream=task_event_sink,
        vu_flow=virtual_user_flow,
        vu_run_id=(str(task.get('_goalRunId') or '')
                   if virtual_user_flow else ''),
        projection=projection,
    )
    turn_persistence.bind(adapter.messages)

    execution_kwargs = launch.execution_kwargs(
        task, subflow_resolver=definition_service.get_definition)
    if virtual_user_flow and recovery_seed is not None and recovery_seed.pending_actors is not None:
        from lib.goal_runs.native_recovery import execute_pending_goal_flow
        outcome = execute_pending_goal_flow(task, definition, launch,
            journal_factory=ports.create_execution_journal, on_event=adapter.on_event,
            execution_kwargs=execution_kwargs)
    else:
        if virtual_user_flow and ports.create_execution_journal is not None:
            journal = ports.create_execution_journal(task, definition, launch)
            execution_kwargs['executor_options']['execution_journal'] = journal
        outcome = execute_flow(
            definition,
            on_event=adapter.on_event,
            **execution_kwargs,
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
        # Full report rides the durable GoalRun transition event; the fold
        # record in conversation settings carries only the bounded excerpt
        # plus a hasFullReport flag so the UI can fetch the rest on demand.
        full_report = _vu_stop_report(adapter.messages)
        task['_goalStopReportFull'] = full_report
        task['_goalStopReport'] = full_report[:GOAL_STOP_REPORT_EXCERPT_CHARS]
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

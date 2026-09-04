"""Canonical failure projection for Flow-backed chat tasks.

Entry selection and execution failures have different user-facing envelopes,
but they must converge through the same chat terminal boundary. This module is
the only adapter between orchestration launch failures and task-manager
finalization; callers consume semantic functions instead of manager internals.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from lib.error_envelope import from_exception, make_envelope
from lib.log import audit_log, get_logger


logger = get_logger(__name__)


@dataclass(frozen=True)
class OrchestrationChatFailurePorts:
    """Side effects required to settle one failed Flow-backed chat task."""

    finalize_error: Callable
    fail_goal_run: Callable | None = None

    @classmethod
    def defaults(cls) -> 'OrchestrationChatFailurePorts':
        """Resolve the production chat terminal boundary lazily."""
        from lib.tasks_pkg.manager import finalize_chat_task_error

        from lib.goal_runs.service import GoalRunService

        return cls(
            finalize_error=finalize_chat_task_error,
            fail_goal_run=lambda task, reason: GoalRunService().fail(
                task, reason=reason),
        )


def _settle_started_goal_failure(
    task: dict,
    *,
    reason: str,
    ports: OrchestrationChatFailurePorts,
) -> None:
    if not task.get('_goalRunId') or ports.fail_goal_run is None:
        return
    try:
        ports.fail_goal_run(task, reason)
    except Exception as goal_error:
        # The original execution failure remains the user-facing cause.  A
        # failed durable transition is logged loudly and startup recovery will
        # retire the still-active physical row; it must not conceal that cause.
        logger.error(
            '[FlowChat] GoalRun failure settlement failed task=%s: %s',
            str(task.get('id') or '')[:8], goal_error, exc_info=True,
        )


def unavailable_selected_flow_reference(config: dict) -> str:
    """Return the stable diagnostic reference for an unavailable selection."""
    if config.get('flowId'):
        return f"stored:{config['flowId']}"
    if config.get('flowBuiltin'):
        return f"builtin:{config['flowBuiltin']}"
    return 'inline'


def finalize_unavailable_orchestration_chat_flow(
    task: dict,
    *,
    ports: OrchestrationChatFailurePorts | None = None,
) -> dict:
    """Fail closed when a user-selected definition cannot be resolved."""
    ports = ports or OrchestrationChatFailurePorts.defaults()
    config = task.get('config') or {}
    selected = unavailable_selected_flow_reference(config)
    task['_flow_label'] = f'flow({selected})'
    error = make_envelope(
        'bad_request',
        message=('所选编排流程不可用\n'
                 'Selected orchestration flow is unavailable'),
        detail=f'Could not resolve selected flow {selected}.',
        model=config.get('model', ''),
        context='orchestration-flow-selection',
        source='lib.orchestration_chat_flow_runner',
        retryable=False,
        hint=('请重新打开“编排流程”选择器并选择一个仍然存在的流程。\n'
              'Reopen the orchestration picker and select an available flow.'),
    )
    logger.warning(
        '[FlowChat] task=%s selected flow %s unavailable — failing closed',
        str(task.get('id') or '')[:8], selected,
    )
    audit_log(
        'flow_via_chat_resolution_failed',
        task_id=task.get('id', ''),
        flow=selected,
    )
    ports.finalize_error(
        task, error, flow_reason='definition_unavailable')
    return error


def finalize_orchestration_chat_flow_exception(
    task: dict,
    error: BaseException,
    *,
    label: str,
    ports: OrchestrationChatFailurePorts | None = None,
) -> dict:
    """Project an unexpected Flow runtime exception into chat termination."""
    ports = ports or OrchestrationChatFailurePorts.defaults()
    logger.error(
        '[FlowChat] task=%s label=%s FATAL error',
        str(task.get('id') or '')[:8], label, exc_info=True,
    )
    audit_log(
        'flow_via_chat_failed',
        task_id=task.get('id', ''),
        flow=label,
        error=type(error).__name__,
    )
    envelope = from_exception(
        error,
        model=(task.get('config') or {}).get('model', ''),
        context='orchestration-flow-fatal',
        source='lib.orchestration_chat_flow_runner',
        kind='internal',
    )
    _settle_started_goal_failure(
        task, reason='runtime_failure', ports=ports)
    ports.finalize_error(task, envelope, flow_reason='fatal')
    return envelope


__all__ = [
    'OrchestrationChatFailurePorts',
    'finalize_orchestration_chat_flow_exception',
    'finalize_unavailable_orchestration_chat_flow',
    'unavailable_selected_flow_reference',
]

"""Machine-readable lifecycle and policy contract for chat Goal Mode.

This module is framework- and storage-neutral.  Flow execution, Sidecar
persistence and chat delivery all consume these values; none owns a private
translation of GoalRun status or long-horizon policy.
"""

from __future__ import annotations

from typing import Final

from lib.orchestration.loop_policy import (
    BUDGET_POLICY_CHECKPOINT_EXTEND,
    MAX_EXECUTOR_MAX_ITERATIONS,
    bounded_executor_iterations,
)


GOAL_RUN_FORMAT: Final = 'tofu.goal-run/v1'
GOAL_RUN_CREATED_BY: Final = 'chat_goal_mode'
GOAL_RUN_ORCHESTRATION_PREFIX: Final = 'chat-goal:'
GOAL_RUN_ID_PREFIX: Final = 'goal_'
MAX_GOAL_OBJECTIVE_CHARS: Final = 48_000
DEFAULT_GOAL_MAX_ITERATIONS: Final = 40
MAX_GOAL_MAX_ITERATIONS: Final = MAX_EXECUTOR_MAX_ITERATIONS


# Input policy is independent of child settlement/cancellation authority. Chat
# Goal workers retain the ordinary model working set; verifiers remain isolated.
GOAL_WORKER_CONTEXT_POLICY: Final = 'conversation'
GOAL_EXECUTION_LAUNCH_FORMAT: Final = 'tofu.goal-execution-launch/v2'
# Older archives remain recognizable so generic Studio recovery cannot consume
# them; they lack invocation authority and never authorize Goal reconstruction.
GOAL_EXECUTION_LAUNCH_FORMATS: Final = (
    'tofu.goal-execution-launch/v1', GOAL_EXECUTION_LAUNCH_FORMAT,
)
GOAL_WORKER_STATE_KEYS: Final = ('_todoState', '_todos')


def goal_execution_launch_contract() -> dict:
    """Schema for the immutable chat launch carried by the shared journal."""
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['format', 'launch_id', 'run_id', 'task_id',
                     'conversation_id', 'owner_user_id', 'tenant_id',
                     'turn_id', 'attempt_id', 'objective', 'launch', 'messages',
                     'worker_state', 'invocation_policy'],
        'properties': {
            'format': {'const': GOAL_EXECUTION_LAUNCH_FORMAT},
            **{name: {'type': 'string', 'minLength': 1} for name in (
                'launch_id', 'run_id', 'task_id', 'conversation_id', 'objective')},
            **{name: {'type': 'string'} for name in (
                'tenant_id', 'turn_id', 'attempt_id')},
            'owner_user_id': {'type': 'integer', 'minimum': 1},
            'launch': {
                'type': 'object', 'additionalProperties': False,
                'required': ['projection', 'initial_phase', 'initial_context',
                             'system_prompt_base', 'tools', 'model',
                             'project_path', 'thinking_enabled', 'max_iterations'],
                'properties': {
                    'projection': {'const': 'autopilot'},
                    **{name: {'type': 'string'} for name in (
                        'initial_phase', 'initial_context', 'system_prompt_base',
                        'model', 'project_path')},
                    'tools': {'type': 'array', 'items': {'type': 'object'}},
                    'thinking_enabled': {'type': ['boolean', 'null']},
                    'max_iterations': {'type': 'integer', 'minimum': 1,
                                       'maximum': MAX_GOAL_MAX_ITERATIONS},
                },
            },
            'messages': {'type': 'array', 'minItems': 1,
                         'items': {'type': 'object'}},
            'worker_state': {'type': 'object', 'additionalProperties': False,
                             'properties': {name: {} for name in GOAL_WORKER_STATE_KEYS}},
            'invocation_policy': {
                'type': 'object', 'additionalProperties': False,
                'required': ['config', 'conversation_tool_policy',
                             'tool_contracts', 'tool_script_safe'],
                'properties': {name: {'type': 'object'} for name in (
                    'config', 'conversation_tool_policy',
                    'tool_contracts', 'tool_script_safe')},
            },
        },
    }

GOAL_RUN_STATUS_ORDER: Final = (
    'active', 'completed', 'blocked', 'failed', 'cancelled',
)
GOAL_RUN_TERMINAL_STATUSES: Final = frozenset(
    {'completed', 'blocked', 'failed', 'cancelled'})

GOAL_RUN_REASON_ORDER: Final = (
    'started',
    'objective_verified',
    'iteration_budget_exhausted',
    'execution_budget_exhausted',
    'no_verified_progress',
    'verifier_declared_blocked',
    'human_stop',
    'superseded_by_human',
    'superseded_by_new_goal',
    'conversation_deleted',
    'runtime_shutdown',
    'worker_lost',
    'execution_unavailable',
    'runtime_failure',
)

GOAL_RUN_REASONS_BY_STATUS: Final = {
    'completed': frozenset({'objective_verified'}),
    'blocked': frozenset({
        # ``iteration_budget_exhausted`` is retained for historical rows:
        # checkpoint-extend runs never stop on budget alone.  ``stalled``
        # loop exits map to ``no_verified_progress`` (a full window with
        # zero state changes and zero verified progress); a gated VU
        # [VERDICT: BLOCKED] maps to ``verifier_declared_blocked``.
        'iteration_budget_exhausted',
        'execution_budget_exhausted',
        'no_verified_progress',
        'verifier_declared_blocked',
    }),
    'failed': frozenset({
        'worker_lost',
        'execution_unavailable',
        'runtime_failure',
    }),
    'cancelled': frozenset({
        'human_stop',
        'superseded_by_human',
        'superseded_by_new_goal',
        'conversation_deleted',
        'runtime_shutdown',
    }),
}

# This sentence is also injected into the canonical worker/VU definitions.
# Policy therefore remains executable guidance, not metadata that can drift
# away from the prompts which actually govern a run.  The final sentences are
# the anti-shrink fidelity rule (ported from Codex's goal-continuation
# steering): a run must never re-scope the objective to what is easy now.
GOAL_POLICY_DIRECTIVE: Final = (
    'Pursue the stated objective for durable long-term benefit. Diagnose and '
    'fix root causes, require concrete verification evidence, and do not '
    'substitute a temporary patch when a robust maintainable solution is '
    'within the delegated scope. Never shrink, narrow, or redefine the '
    'objective to fit what is easy to finish now: work is aligned only when '
    'it makes the full requested end state more true, and a smaller, safer, '
    'or easier-to-verify substitute that preserves a different end state is '
    'misaligned. Completion means the complete original scope is true and '
    'verified, not the largest subset that fits the moment.'
)


def goal_run_policy() -> dict:
    """Return a detached policy snapshot stored with every GoalRun start."""
    return {
        'solutionHorizon': 'long_term',
        'rootCauseRequired': True,
        'verificationEvidenceRequired': True,
        'temporaryPatchPolicy': 'reject_when_robust_solution_is_in_scope',
        'objectiveFidelity': 'never_shrink_original_scope',
        'iterationBudget': {
            # A budget window is authorized work, never a stop reason: a
            # closing window that still advances is extended automatically;
            # only a full window with zero state changes AND zero verified
            # progress (the stall breaker) or a gated VU impasse verdict
            # ends the loop as blocked.
            'windowDefault': DEFAULT_GOAL_MAX_ITERATIONS,
            'windowHardCeiling': MAX_GOAL_MAX_ITERATIONS,
            'policy': BUDGET_POLICY_CHECKPOINT_EXTEND,
            'stallBreaker': (
                'zero_state_changes_and_zero_verified_progress_in_window'),
        },
        'workerContext': {
            'policy': GOAL_WORKER_CONTEXT_POLICY,
            'history': 'model_messages_with_tool_results',
            'compaction': 'ordinary_chat',
            'reviewer': 'isolated_latest_work',
        },
        'directive': GOAL_POLICY_DIRECTIVE,
    }


def goal_iteration_budget(value: object = None) -> int:
    """Return Goal Mode's durable default within the shared hard ceiling."""
    return bounded_executor_iterations(
        value, default=DEFAULT_GOAL_MAX_ITERATIONS)


def goal_run_contract() -> dict:
    """Return the transport-safe GoalRun state-machine contract."""
    return {
        'format': GOAL_RUN_FORMAT,
        'initialStatus': 'active',
        'statuses': list(GOAL_RUN_STATUS_ORDER),
        'terminalStatuses': [
            status for status in GOAL_RUN_STATUS_ORDER
            if status in GOAL_RUN_TERMINAL_STATUSES
        ],
        'reasons': list(GOAL_RUN_REASON_ORDER),
        'reasonsByStatus': {
            status: [
                reason for reason in GOAL_RUN_REASON_ORDER
                if reason in GOAL_RUN_REASONS_BY_STATUS[status]
            ]
            for status in GOAL_RUN_STATUS_ORDER
            if status in GOAL_RUN_TERMINAL_STATUSES
        },
        'objectiveSource': 'accepted_human_turn',
        'executionOwner': 'flow_executor',
        'policy': goal_run_policy(),
    }


def goal_run_contract_schema() -> dict:
    """Return an OpenAPI-compatible schema of the live contract snapshot."""
    from lib.orchestration.contract_schema import contract_snapshot_schema

    return contract_snapshot_schema(goal_run_contract())


def is_valid_goal_transition(status: object, reason: object) -> bool:
    """Return whether a terminal status/reason pair has defined meaning."""
    return (
        isinstance(status, str)
        and isinstance(reason, str)
        and reason in GOAL_RUN_REASONS_BY_STATUS.get(status, frozenset())
    )


def goal_orchestration_id(conversation_id: str) -> str:
    """Return the owner-scoped physical-run grouping key for a conversation."""
    conversation_id = str(conversation_id or '').strip()
    if not conversation_id:
        raise ValueError('GoalRun requires a conversation id')
    return GOAL_RUN_ORCHESTRATION_PREFIX + conversation_id


def goal_run_id_for_task(task_id: str) -> str:
    """Derive an idempotent GoalRun identity from the bound executor task."""
    task_id = str(task_id or '').strip()
    if not task_id:
        raise ValueError('GoalRun requires an executor task id')
    return GOAL_RUN_ID_PREFIX + task_id


def storage_status_for_goal_status(status: str) -> str:
    """Project GoalRun meaning onto the existing durable-run header."""
    return {
        'active': 'running',
        'completed': 'done',
        'blocked': 'error',
        'failed': 'error',
        'cancelled': 'aborted',
    }[status]


def goal_status_from_storage(status: str) -> str:
    """Conservative fallback when a historical row has no GoalRun event."""
    return {
        'pending': 'active',
        'running': 'active',
        'paused': 'blocked',
        'done': 'completed',
        'error': 'failed',
        'aborted': 'cancelled',
    }.get(str(status or ''), 'failed')


def terminal_goal_transition(
    category: str,
    *,
    stop_reason: str = '',
    abort_reason: str = '',
) -> tuple[str, str]:
    """Classify one orchestration terminal outcome into GoalRun meaning."""
    category = str(category or '')
    stop = str(stop_reason or '').strip().lower()
    abort = str(abort_reason or '').strip().lower()
    if category == 'success':
        return 'completed', 'objective_verified'
    if category == 'incomplete':
        if 'vu_blocked' in stop:
            return 'blocked', 'verifier_declared_blocked'
        if 'max_iteration' in stop or 'iteration' in stop:
            return 'blocked', 'iteration_budget_exhausted'
        if 'budget' in stop:
            return 'blocked', 'execution_budget_exhausted'
        return 'blocked', 'no_verified_progress'
    if category == 'aborted':
        if 'supersed' in abort:
            return 'cancelled', 'superseded_by_human'
        if 'conversation_deleted' in abort:
            return 'cancelled', 'conversation_deleted'
        if 'worker_lost' in abort:
            return 'failed', 'worker_lost'
        if 'shutdown' in abort:
            return 'cancelled', 'runtime_shutdown'
        return 'cancelled', 'human_stop'
    if 'worker_lost' in stop:
        return 'failed', 'worker_lost'
    if 'unavailable' in stop or 'definition' in stop:
        return 'failed', 'execution_unavailable'
    return 'failed', 'runtime_failure'


__all__ = [
    'GOAL_POLICY_DIRECTIVE',
    'DEFAULT_GOAL_MAX_ITERATIONS',
    'GOAL_RUN_CREATED_BY',
    'GOAL_EXECUTION_LAUNCH_FORMAT',
    'GOAL_EXECUTION_LAUNCH_FORMATS',
    'GOAL_WORKER_STATE_KEYS',
    'GOAL_RUN_FORMAT',
    'GOAL_RUN_ID_PREFIX',
    'GOAL_RUN_ORCHESTRATION_PREFIX',
    'GOAL_RUN_REASON_ORDER',
    'GOAL_RUN_REASONS_BY_STATUS',
    'GOAL_RUN_STATUS_ORDER',
    'GOAL_RUN_TERMINAL_STATUSES',
    'MAX_GOAL_OBJECTIVE_CHARS',
    'MAX_GOAL_MAX_ITERATIONS',
    'goal_iteration_budget',
    'goal_execution_launch_contract',
    'is_valid_goal_transition',
    'goal_orchestration_id',
    'goal_run_contract',
    'goal_run_contract_schema',
    'goal_run_id_for_task',
    'goal_run_policy',
    'goal_status_from_storage',
    'storage_status_for_goal_status',
    'terminal_goal_transition',
]

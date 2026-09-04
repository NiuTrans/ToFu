"""lib/orchestration_chat_flow_runner.py — Chat flows via FlowExecutor.

The convergence point where goal mode (autopilot worker ⇄ VU graph) AND
arbitrary user-authored Studio flows all run through ONE engine
(:class:`lib.orchestration_engine.FlowExecutor`) and ONE translator
(:class:`lib.orchestration_chat_flow_adapter.FlowEventAdapter` → flow
SSE/message schema, so the existing frontend renders every flow unchanged).

Entry points (both share :func:`_run_flow_as_chat_task`):
  * :func:`run_autopilot_via_flow` — canonical goal-mode (worker ⇄ VU) graph.
  * :func:`run_flow_via_chat`      — a user-SELECTED flow (inline / builtin /
    stored id) resolved by :func:`resolve_chat_flow_definition`.

``lib/conversation_sync/task_start.py`` calls :func:`resolve_chat_flow_entry`
to pick one (or ``None`` → fall back to the live path / a normal task).

Goal Mode has no fallback interpreter or rollout flag.  Every newly accepted
goal turn runs through this owner.
"""

from __future__ import annotations


from lib.orchestration_chat_launch import (
    build_flow_initial_context,
    build_tools_for_chat_task,
    extract_system_prompt,
    extract_user_request,
)
from lib.orchestration_chat_flow_runtime import (
    execute_orchestration_chat_flow_task,
)
from lib.orchestration_chat_failure import (
    finalize_orchestration_chat_flow_exception,
    finalize_unavailable_orchestration_chat_flow,
)
from lib.orchestration_chat_flow_selection import (
    CHAT_FLOW_ENTRY_AUTOPILOT,
    CHAT_FLOW_ENTRY_SELECTED,
    resolve_chat_flow_definition as _resolve_chat_flow_definition,
    select_chat_flow_entry,
)
from lib.goal_runs.contract import goal_iteration_budget
from lib.orchestration.loop_policy import DEFAULT_EXECUTOR_MAX_ITERATIONS

def _authoring_service():
    """Build the authoring capability used by non-HTTP chat launches."""
    from lib.orchestration.authoring_service import OrchestrationAuthoringService
    return OrchestrationAuthoringService()


def _build_builtin(name: str, *, authoring_service=None, **kwargs) -> dict:
    """Resolve canonical chat-mode graphs through the application service."""
    authoring = authoring_service or _authoring_service()
    definition = authoring.build_builtin(name, **kwargs)
    if definition is None:  # Static built-in names below make this defensive.
        raise ValueError(f'Unknown orchestration builtin {name!r}')
    return definition


def _definition_service(
    owner_user_id: int, *, tenant_id: str | None = None,
):
    """Build the one definition application boundary for a chat launch."""
    from lib.orchestration.definition_service import (
        OrchestrationDefinitionService,
    )
    return OrchestrationDefinitionService.for_owner(
        owner_user_id, tenant_id=tenant_id)


def _task_repository_identity(task: dict) -> tuple[int, str | None]:
    """Read the immutable owner snapshot carried by the executor task."""
    from lib.tasks_pkg.manager._registry import task_user_id

    return task_user_id(task), task.get('_tenant_id')


def resolve_chat_flow_definition(
    config: dict,
    *,
    definition_service=None,
    owner_user_id: int | None = None,
    tenant_id: str | None = None,
) -> tuple[dict | None, str]:
    """Resolve a chat task's selected flow into a definition + source label.

    Precedence: inline ``flowDefinition`` → ``flowBuiltin`` name
    (autopilot) → stored ``flowId``. Returns ``(defn, source)`` or
    ``(None, '')`` when no flow is selected.
    """
    if definition_service is None:
        if not config.get('flowId'):
            from lib.orchestration.definition_resolution import (
                resolve_definition,
            )
            resolved = resolve_definition(
                inline=config.get('flowDefinition'),
                builtin=(config.get('flowBuiltin')
                         if isinstance(config.get('flowBuiltin'), str) else ''),
                require_inline_nodes=True,
            )
            return resolved.definition, resolved.source
        if owner_user_id is None:
            raise ValueError(
                'stored chat flow resolution requires owner_user_id')
        definition_service = _definition_service(
            owner_user_id, tenant_id=tenant_id)
    return _resolve_chat_flow_definition(
        config,
        definition_service=definition_service,
    )


def resolve_chat_flow_entry(config: dict):
    """Pick the FlowExecutor entry point for a chat task, or ``None``.

    Encapsulates ALL the dispatch/flag logic so the caller stays a
    thin switch:

      1. An explicit flow selection (``flowDefinition`` / ``flowBuiltin`` /
         ``flowId``) → :func:`run_flow_via_chat` (honored whenever the user
         selects a flow; no flag, the selection is the opt-in). The
         "编排流程" dropdown is another deliberate projection of the same
         FlowExecutor owner used by the Goal Mode toggle.
      2. ``autopilot`` → :func:`run_autopilot_via_flow`.

    Returns a ``callable(task)`` or ``None`` (caller uses a normal task).
    """
    kind = select_chat_flow_entry(config)
    if kind == CHAT_FLOW_ENTRY_SELECTED:
        return run_flow_via_chat
    if kind == CHAT_FLOW_ENTRY_AUTOPILOT:
        return run_autopilot_via_flow
    return None


def _extract_user_request(task: dict) -> str:
    """Compatibility facade for the extracted launch boundary."""
    return extract_user_request(task)


def _extract_system_prompt(task: dict) -> str:
    """Compatibility facade for the extracted launch boundary."""
    return extract_system_prompt(task)


def _build_flow_initial_context(task: dict) -> str:
    """Compatibility facade for the extracted launch boundary."""
    return build_flow_initial_context(task)


def _build_tools_for_task(task: dict):
    """Compatibility facade for the extracted launch boundary."""
    return build_tools_for_chat_task(task)


def run_autopilot_via_flow(task: dict):
    """Run goal mode (autopilot) through its sole FlowExecutor path.

    Runs the canonical autopilot graph (``build_autopilot_definition`` —
    worker ⇄ virtual_user loop) on the unified engine. The virtual_user's
    turns surface as user-side messages via the adapter's ``emits`` handling,
    so the existing chat UI renders the synthetic-user replies with no
    frontend change.
    """
    cfg = task.get('config') or {}
    max_iter = goal_iteration_budget(cfg.get('autopilotMaxIterations'))
    _run_flow_as_chat_task(
        task, _build_builtin('autopilot', max_iterations=max_iter),
        label='autopilot', max_iter=max_iter)


def run_flow_via_chat(task: dict):
    """Run a USER-SELECTED orchestration flow as a chat task.

    The flow is resolved from the task config (inline ``flowDefinition`` /
    ``flowBuiltin`` name / stored ``flowId``) by
    :func:`resolve_chat_flow_definition`. This is the capability that lets a
    flow authored in the Studio drive a real conversation — the final
    convergence point where goal mode AND arbitrary custom flows all run
    through one engine + one adapter.
    """
    cfg = task.get('config') or {}
    owner_user_id, tenant_id = _task_repository_identity(task)
    definitions = _definition_service(
        owner_user_id, tenant_id=tenant_id)
    defn, source = resolve_chat_flow_definition(
        cfg, definition_service=definitions)
    if defn is None:
        # Fail closed: silently substituting another graph here executes a
        # DIFFERENT graph from the one the user selected.  The shared chat
        # terminal boundary makes the mismatch visible and settles every
        # event/persistence/busy projection consistently.
        finalize_unavailable_orchestration_chat_flow(task)
        return
    from lib.orchestration._chat_projection import chat_projection_for_flow

    if chat_projection_for_flow(defn) == 'autopilot':
        max_iter = goal_iteration_budget(
            cfg.get('flowMaxIterations')
            or cfg.get('autopilotMaxIterations'))
    else:
        max_iter = int(
            cfg.get('flowMaxIterations')
            or DEFAULT_EXECUTOR_MAX_ITERATIONS)
    _run_flow_as_chat_task(
        task,
        defn,
        label=f'flow({source})',
        max_iter=max_iter,
        definition_service=definitions,
    )


def _run_flow_as_chat_task(task: dict, defn: dict, *, label: str,
                           max_iter: int, definition_service=None,
                           goal_run_service=None):
    """Run one Flow-backed chat task behind the canonical fatal boundary."""
    try:
        from lib.orchestration._chat_projection import chat_projection_for_flow

        if chat_projection_for_flow(defn) == 'autopilot':
            if goal_run_service is None:
                from lib.goal_runs.service import GoalRunService
                goal_run_service = GoalRunService()
            goal_run_service.start(task, defn)
        return _execute_flow_as_chat_task(
            task,
            defn,
            label=label,
            max_iter=max_iter,
            definition_service=definition_service,
        )
    except Exception as error:
        finalize_orchestration_chat_flow_exception(
            task, error, label=label)
        return None


def _execute_flow_as_chat_task(
    task: dict,
    defn: dict,
    *,
    label: str,
    max_iter: int,
    definition_service=None,
):
    """Compatibility patch point over the canonical chat-flow runtime."""
    if definition_service is None:
        owner_user_id, tenant_id = _task_repository_identity(task)
        definition_service = _definition_service(
            owner_user_id, tenant_id=tenant_id)
    return execute_orchestration_chat_flow_task(
        task,
        defn,
        label=label,
        max_iterations=max_iter,
        definition_service=definition_service,
        tool_builder=_build_tools_for_task,
        context_builder=_build_flow_initial_context,
        system_prompt_builder=_extract_system_prompt,
    )


__all__ = [
    'run_autopilot_via_flow',
    'run_flow_via_chat', 'resolve_chat_flow_definition', 'resolve_chat_flow_entry',
]

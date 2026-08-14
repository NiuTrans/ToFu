"""lib/orchestration_endpoint_runner.py — Chat modes via FlowExecutor.

The convergence point where endpoint mode, autopilot mode, AND arbitrary
user-authored Studio flows all run through ONE engine
(:class:`lib.orchestration_engine.FlowExecutor`) and ONE translator
(:class:`lib.orchestration_endpoint_adapter.EndpointEventAdapter` → endpoint
SSE/message schema, so the existing frontend renders every mode unchanged).

Entry points (all share :func:`_run_flow_as_endpoint_task`):
  * :func:`run_endpoint_via_flow`  — canonical endpoint graph.
  * :func:`run_autopilot_via_flow` — canonical autopilot (worker ⇄ VU) graph.
  * :func:`run_flow_via_chat`      — a user-SELECTED flow (inline / builtin /
    stored id) resolved by :func:`resolve_chat_flow_definition`.

``routes/chat.py`` calls :func:`resolve_chat_flow_entry` to pick one (or
``None`` → fall back to the live path / a normal task).

Flags (each default OFF, symmetric):
    TOFU_ENDPOINT_VIA_FLOW=1    → endpoint mode uses this engine path
    TOFU_AUTOPILOT_VIA_FLOW=1   → autopilot mode uses this engine path
    (a user-selected flow is ALWAYS honored — the selection is the opt-in)

The live ``lib/tasks_pkg/endpoint.py`` / ``autopilot.py`` paths remain the
default + authoritative until each flagged path is validated on real tasks.
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
    CHAT_FLOW_ENTRY_ENDPOINT,
    CHAT_FLOW_ENTRY_SELECTED,
    autopilot_via_flow_enabled,
    endpoint_via_flow_enabled,
    resolve_chat_flow_definition as _resolve_chat_flow_definition,
    select_chat_flow_entry,
)
from lib.orchestration.loop_policy import (
    DEFAULT_EXECUTOR_MAX_ITERATIONS,
    DEFAULT_MAX_ITERATIONS,
)

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


def _definition_service():
    """Build the one definition application boundary for a chat launch."""
    from lib.orchestration.definition_service import (
        OrchestrationDefinitionService,
    )
    return OrchestrationDefinitionService.from_path()


def resolve_chat_flow_definition(
    config: dict,
    *,
    definition_service=None,
) -> tuple[dict | None, str]:
    """Resolve a chat task's selected flow into a definition + source label.

    Precedence: inline ``flowDefinition`` → ``flowBuiltin`` name
    (endpoint|autopilot) → stored ``flowId``. Returns ``(defn, source)`` or
    ``(None, '')`` when no flow is selected.
    """
    if definition_service is None:
        definition_service = _definition_service()
    return _resolve_chat_flow_definition(
        config,
        definition_service=definition_service,
    )


def resolve_chat_flow_entry(config: dict):
    """Pick the FlowExecutor entry point for a chat task, or ``None``.

    Encapsulates ALL the dispatch/flag logic so ``routes/chat.py`` stays a
    thin switch:

      1. An explicit flow selection (``flowDefinition`` / ``flowBuiltin`` /
         ``flowId``) → :func:`run_flow_via_chat` (a NEW capability — honored
         whenever the user selects a flow; no flag, the selection is the
         opt-in). This is SYMMETRIC across builtins: BOTH
         ``flowBuiltin='endpoint'`` and ``flowBuiltin='autopilot'`` run on the
         FlowExecutor engine. The "编排流程" dropdown is the deliberate way to
         exercise the ENGINE implementation of each mode (so engine bugs are
         observable in the frontend), distinct from the "模式" toggles which
         drive the live ``tasks_pkg`` implementations.
      2. ``endpointMode`` + ``TOFU_ENDPOINT_VIA_FLOW`` → :func:`run_endpoint_via_flow`.
      3. ``autopilot`` + ``TOFU_AUTOPILOT_VIA_FLOW`` → :func:`run_autopilot_via_flow`.

    The ``TOFU_*_VIA_FLOW`` flags govern ONLY the "模式" TOGGLE paths (2/3):
    with the flag OFF the toggle uses the live ``tasks_pkg`` loop; ON reroutes
    it through the engine. A dropdown flow SELECTION (1) is always the engine,
    flag-independent.

    Returns a ``callable(task)`` or ``None`` (caller falls back to the live
    endpoint path or a normal task).
    """
    kind = select_chat_flow_entry(
        config,
        endpoint_enabled=endpoint_via_flow_enabled,
        autopilot_enabled=autopilot_via_flow_enabled,
    )
    if kind == CHAT_FLOW_ENTRY_SELECTED:
        return run_flow_via_chat
    if kind == CHAT_FLOW_ENTRY_ENDPOINT:
        return run_endpoint_via_flow
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


def run_endpoint_via_flow(task: dict):
    """Run endpoint mode through FlowExecutor (flagged path).

    Thin wrapper over :func:`_run_flow_as_endpoint_task` with the canonical
    endpoint graph (``build_endpoint_definition``).
    """
    cfg = task.get('config') or {}
    max_iter = int(
        cfg.get('endpointMaxIterations') or DEFAULT_MAX_ITERATIONS)
    _run_flow_as_endpoint_task(
        task, _build_builtin('endpoint', max_iterations=max_iter),
        label='endpoint', max_iter=max_iter)


def run_autopilot_via_flow(task: dict):
    """Run autopilot mode through FlowExecutor (flagged path).

    Symmetric to :func:`run_endpoint_via_flow`: runs the canonical autopilot
    graph (``build_autopilot_definition`` — worker ⇄ virtual_user loop) on
    the unified engine. The virtual_user's turns surface as user-side
    messages via the adapter's ``emits`` handling, so the existing chat UI
    renders the synthetic-user replies with no frontend change.
    """
    cfg = task.get('config') or {}
    max_iter = int(cfg.get('autopilotMaxIterations')
                   or cfg.get('endpointMaxIterations')
                   or DEFAULT_EXECUTOR_MAX_ITERATIONS)
    _run_flow_as_endpoint_task(
        task, _build_builtin('autopilot', max_iterations=max_iter),
        label='autopilot', max_iter=max_iter)


def run_flow_via_chat(task: dict):
    """Run a USER-SELECTED orchestration flow as a chat task.

    The flow is resolved from the task config (inline ``flowDefinition`` /
    ``flowBuiltin`` name / stored ``flowId``) by
    :func:`resolve_chat_flow_definition`. This is the capability that lets a
    flow authored in the Studio drive a real conversation — the final
    convergence point where endpoint, autopilot, AND arbitrary custom flows
    all run through one engine + one adapter.
    """
    cfg = task.get('config') or {}
    definitions = _definition_service()
    defn, source = resolve_chat_flow_definition(
        cfg, definition_service=definitions)
    if defn is None:
        # Fail closed: silently substituting endpoint here executes a
        # DIFFERENT graph from the one the user selected.  The shared chat
        # terminal boundary makes the mismatch visible and settles every
        # event/persistence/busy projection consistently.
        finalize_unavailable_orchestration_chat_flow(task)
        return
    max_iter = int(
        cfg.get('endpointMaxIterations')
        or DEFAULT_EXECUTOR_MAX_ITERATIONS)
    _run_flow_as_endpoint_task(
        task,
        defn,
        label=f'flow({source})',
        max_iter=max_iter,
        definition_service=definitions,
    )


def _run_flow_as_endpoint_task(task: dict, defn: dict, *, label: str,
                               max_iter: int, definition_service=None):
    """Run one Flow-backed chat task behind the canonical fatal boundary."""
    try:
        return _execute_flow_as_endpoint_task(
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


def _execute_flow_as_endpoint_task(
    task: dict,
    defn: dict,
    *,
    label: str,
    max_iter: int,
    definition_service=None,
):
    """Compatibility patch point over the canonical chat-flow runtime."""
    return execute_orchestration_chat_flow_task(
        task,
        defn,
        label=label,
        max_iterations=max_iter,
        definition_service=definition_service or _definition_service(),
        tool_builder=_build_tools_for_task,
        context_builder=_build_flow_initial_context,
        system_prompt_builder=_extract_system_prompt,
    )


__all__ = [
    'run_endpoint_via_flow', 'endpoint_via_flow_enabled',
    'run_autopilot_via_flow', 'autopilot_via_flow_enabled',
    'run_flow_via_chat', 'resolve_chat_flow_definition', 'resolve_chat_flow_entry',
]

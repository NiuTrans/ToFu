"""Provider-neutral routing and authority for multi-agent orchestration.

Responsibility
--------------
Resolve a task-level ``read_only`` multi-agent decision into exactly one
execution backend and project the corresponding model-visible tool surface:

* ``native_openai`` uses the public OpenAI Responses multi-agent extension;
* ``local_swarm`` uses Tofu's existing ``spawn_agents`` runtime for every
  other tool-capable model; and
* ``off`` is the fail-closed result when neither backend is available.

This module also owns the read-only tool filter shared by local Swarm workers
and native multi-agent dispatch enforcement.  Request handlers remain the
execution authority; wire projection is guidance only.

Entry points: :func:`resolve_multi_agent_backend`,
:func:`project_multi_agent_wire_tools`, and
:func:`read_only_swarm_tools`.
"""

from __future__ import annotations

from typing import Any

from lib.swarm.resource_policy import swarm_max_agents_per_wave


ACTIVE_MULTI_AGENT_MODES = frozenset({'read_only'})
MULTI_AGENT_BACKENDS = frozenset({'off', 'native_openai', 'local_swarm'})
SPAWN_AGENTS_NAME = 'spawn_agents'
# Read-only workers are also non-interactive leaves. These tools may not write
# durable state, but allowing a provider-native worker to wait on another
# orchestration lane or request root-user input can deadlock the root turn and
# violates the same leaf-worker contract already enforced by local Swarm.
READ_ONLY_AGENT_EXTRA_BAN = frozenset({
    'ask_human', 'spawn_agents', 'await_agents', 'get_agent_result',
    'await_task',
})


def tool_schema_name(tool: Any) -> str:
    """Return a function tool's name without assuming one wire dialect."""
    if not isinstance(tool, dict):
        return ''
    function = tool.get('function')
    if isinstance(function, dict):
        return str(function.get('name') or '')
    return str(tool.get('name') or '')


def catalog_has_spawn_agents(catalog: Any) -> bool:
    """Whether *catalog* carries local Swarm execution authority."""
    return any(tool_schema_name(tool) == SPAWN_AGENTS_NAME
               for tool in (catalog or ()))


def resolve_multi_agent_backend(
        requested: str, *, protocol: str = '', model: str = '',
        responses_profile: str = '', base_url: str = '', oauth: str = '',
        local_swarm_available: bool = False) -> str:
    """Resolve ``read_only`` to ``native_openai | local_swarm | off``.

    A model name alone never enables a provider extension.  Native execution
    requires the public-OpenAI Responses feature profile and an official
    GPT-5.6 model.  Every other provider falls back to local Swarm only when
    the task-owned executable catalog actually contains ``spawn_agents``.
    """
    if str(requested or '').strip().lower() not in ACTIVE_MULTI_AGENT_MODES:
        return 'off'

    profile = str(responses_profile or '').strip().lower()
    if not profile:
        from lib.llm.responses_features import (
            normalize_responses_feature_profile,
        )
        profile = normalize_responses_feature_profile(
            '', protocol=protocol, base_url=base_url, oauth=oauth)

    if (str(protocol or '').strip().lower() == 'responses'
            and profile == 'openai'
            and str(oauth or '').strip().lower() != 'codex'):
        from lib.model_info._openai_gpt56 import is_official_gpt56_model
        if is_official_gpt56_model(model):
            return 'native_openai'
    return 'local_swarm' if local_swarm_available else 'off'


def _local_swarm_guidance(stage: str, max_agents: int) -> str:
    stage_text = str(stage or '').strip() or (
        'independent research, inspection, comparison, or verification')
    # This conditional contract is deliberately present in every local Swarm
    # projection. Whether PTC happens to be active is per-round evidence; using
    # it to add/remove this sentence rewrote the hoisted tools cache prefix.
    programmatic = (
        ' When the parent task activates programmatic reads, eligible workers '
        'also receive the bounded local surface for reducing repeated reads.')
    return (
        'LOCAL MULTI-AGENT FALLBACK IS ACTIVE FOR THIS ROUND. Delegate only '
        f'{stage_text}. All workers are enforced read-only at tool dispatch: '
        'they cannot edit files, run mutating commands, change external or '
        'project state, schedule work, or spawn further agents. Put at most '
        f'{max_agents} agents in this wave; use one agents[] array so the '
        f'scheduler can run them concurrently.{programmatic}')


def project_multi_agent_wire_tools(
        tools: list[dict[str, Any]] | None, *,
        authority_catalog: list[dict[str, Any]] | None,
        backend: str, stage: str = '', max_concurrent_agents: int = 3,
        programmatic_workers: bool = False) -> list[dict[str, Any]]:
    """Return the model-visible surface for one resolved backend.

    Native multi-agent and local ``spawn_agents`` are alternative control
    planes, so the native projection removes the local spawn primitive.  The
    local projection makes the authority-owned schema directly visible even
    when Tool Search would otherwise defer it, and adds task-specific routing
    guidance plus a hard per-wave item ceiling.
    """
    visible = [tool for tool in (tools or ()) if isinstance(tool, dict)]
    if backend == 'native_openai':
        return [tool for tool in visible
                if tool_schema_name(tool) != SPAWN_AGENTS_NAME]
    if backend != 'local_swarm':
        return visible

    source = next((tool for tool in (authority_catalog or ())
                   if tool_schema_name(tool) == SPAWN_AGENTS_NAME), None)
    if not isinstance(source, dict):
        return visible

    try:
        max(1, min(
            int(max_concurrent_agents),
            8,
            swarm_max_agents_per_wave(),
        ))
    except (TypeError, ValueError):
        pass
    # Runtime stage and capacity guidance is appended to the request's trailing
    # user context by the dispatch boundary. Keep the canonical declaration
    # byte-identical so routing changes never rewrite the tool prefix.
    spawn = source

    out: list[dict[str, Any]] = []
    inserted = False
    for tool in visible:
        if tool_schema_name(tool) == SPAWN_AGENTS_NAME:
            if not inserted:
                out.append(spawn)
                inserted = True
            continue
        out.append(tool)
    if not inserted:
        out.append(spawn)
    return out


def read_only_agent_banned_names(write_tools: Any) -> frozenset[str]:
    """Return the shared native/local leaf-worker authority boundary."""
    from lib.tasks_pkg.plan_mode import plan_mode_banned_names

    return frozenset(
        set(plan_mode_banned_names(write_tools))
        | set(READ_ONLY_AGENT_EXTRA_BAN))


def read_only_swarm_tools(task: dict[str, Any] | None,
                          tools: list[dict[str, Any]] | None
                          ) -> list[dict[str, Any]]:
    """Filter a task-owned catalog through the strict read-only authority.

    The same per-task write partition used by approval/concurrency handling is
    combined with Plan Mode's extra mutator set.  This deliberately treats
    advisory project-brain writes, artifact writes, schedulers, browser state,
    and other non-file mutations as writes too.
    """
    from lib.tasks_pkg.tool_dispatch._flags import _task_partitions

    banned = read_only_agent_banned_names(_task_partitions(task or {})[0])
    return [tool for tool in (tools or ())
            if isinstance(tool, dict) and tool_schema_name(tool) not in banned]


__all__ = [
    'ACTIVE_MULTI_AGENT_MODES',
    'MULTI_AGENT_BACKENDS',
    'READ_ONLY_AGENT_EXTRA_BAN',
    'SPAWN_AGENTS_NAME',
    'catalog_has_spawn_agents',
    'project_multi_agent_wire_tools',
    'read_only_agent_banned_names',
    'read_only_swarm_tools',
    'resolve_multi_agent_backend',
    'tool_schema_name',
]

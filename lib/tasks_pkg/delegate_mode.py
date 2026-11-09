"""lib/tasks_pkg/delegate_mode.py — Delegate Mode: the root agent only orchestrates.

Delegate Mode is one choice in the interaction-mode radio
(``cfg['delegateMode']``, a strict boolean like ``planMode``). The root model
owns the logic flow — understand, decompose, delegate, check, answer — while
``spawn_agents`` workers perform every read, search, command, web lookup and
edit. The root transcript therefore grows by objectives and worker results,
never by raw tool output, so same-shape requests keep a near-identical root
context.

Entry points:

* ``delegate_mode_enabled(cfg)`` — the request declared the mode.
* ``narrow_root_to_delegate_surface(cfg, task, tool_list, apply_policy)`` —
  assembly. Captures the ordinary assembled tool policy as the *delegation
  view*, then re-applies the same assembly policy with the root narrowed to
  ``DELEGATE_ROOT_TOOLS``. Tool Search, PTC and the context composer see only
  what the root may call.
* ``delegate_call_rejection(task, name, arguments)`` — dispatch authority at
  every execution entry (pipeline, executor, streaming prefetch, approval API),
  including each member of an ``execute_tools`` batch.
* ``delegation_policy_view(task)`` / ``delegation_tool_catalog(task)`` — the
  policy delegated workers inherit. Outside Delegate Mode this is the task
  itself, so every delegation boundary uses one accessor in all modes.
* ``delegate_mode_prompt_block()`` — the ``<delegate_mode>`` contract that
  replaces the generic ``<parallel_execution>`` guidance.

Workers never inherit the mode: ``delegateMode`` is not in the child config
inheritance set, and child invocations never carry the captured view. The
root never runs programs (gateway program action, PTC lane and its guidance
all key on ``delegate_mode_active``); workers keep the user's PTC setting.
Dependencies: lib.swarm.routing (tool names).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'DELEGATE_ROOT_TOOLS',
    'DELEGATION_VIEW_KEY',
    'delegate_call_rejection',
    'delegate_mode_active',
    'delegate_mode_enabled',
    'delegate_mode_prompt_block',
    'delegable_tool_names',
    'delegation_config_view',
    'delegation_policy_view',
    'delegation_tool_catalog',
    'narrow_root_to_delegate_surface',
]

#: The root's complete authority in Delegate Mode: launch/steer/read workers,
#: exchange artifacts, consult and record procedure knowledge (skills,
#: memories), talk to the human, and recover its own oversized results.
#: Everything that observes or changes the workspace or the outside world
#: belongs to a worker.
DELEGATE_ROOT_TOOLS = frozenset({
    'spawn_agents', 'get_agent_result', 'send_agent_message', 'get_agent_messages',
    'manage_agents', 'list_agents', 'poll', 'kill',
    'store_artifact', 'read_artifact', 'list_artifacts',
    'search_skills', 'load_skill', 'read_skill_resource',
    'search_memories', 'create_memory', 'update_memory', 'delete_memory',
    'merge_memories',
    'read_tool_artifact', 'search_tool_artifact',
    'ask_human', 'todo_write',
    # Gateway transport only: execute_tools may batch the names above, never a
    # program; search_tools can only find this narrowed catalog.
    'search_tools', 'execute_tools',
})

#: Task field holding the pre-narrowing policy that workers inherit.
DELEGATION_VIEW_KEY = '_delegationToolPolicy'

# Task fields written by ordinary tool assembly that describe tool authority
# and discovery. The delegation view restores them for child derivation.
_TASK_POLICY_FIELDS = (
    '_executable_tool_catalog', '_tool_schema', '_toolExecutionScope', '_toolDiscoveryPolicyByName', '_requiredDirectToolNames',
    '_toolSearchCatalogSize', '_toolSearchableCount', '_toolSearchMode',
    '_toolScriptSafeByName', '_executableToolNamespaceByName',
    '_executableToolSearchTextByName', '_toolContractDocumentsByName',
    '_toolExposureTelemetry', '_frontendSelectedToolNames', '_toolNamespaceByName',
    '_toolInitialVisibleNames',
)
# Config fields the root narrowing rewrites, plus every ``*ByName`` metadata
# map. Ordinary assembly rewrites all of them from the registry, so a narrowed
# config never leaks into a later assembly. User experiment settings (``tools``)
# are never rewritten: root PTC suppression keys on ``delegate_mode_active``.
_CONFIG_POLICY_FIELDS = (
    '_executableToolCatalog', '_requiredDirectToolNames', '_toolSearchCatalogSize',
    '_toolSearchableCount', '_frontendSelectedToolNames', '_toolInitialVisibleNames',
)


def _is_policy_config_field(key: str, value: Any) -> bool:
    return key in _CONFIG_POLICY_FIELDS or (
        key.endswith('ByName') and isinstance(value, dict))


def delegate_mode_enabled(cfg: Mapping | None) -> bool:
    """Whether this request declared Delegate Mode (``delegateMode: true``).

    Strict bool read, matching ``planMode``: absent/None/garbage means off.
    """
    return isinstance(cfg, Mapping) and cfg.get('delegateMode') is True


def delegate_mode_active(task: Mapping | None) -> bool:
    """Whether assembly actually narrowed this task to the delegate surface."""
    view = (task or {}).get(DELEGATION_VIEW_KEY)
    return isinstance(view, dict) and bool(view)


def _tool_name(tool: Any) -> str:
    from lib.swarm.routing import tool_schema_name
    return tool_schema_name(tool)


def _copy_value(value: Any) -> Any:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


def narrow_root_to_delegate_surface(
        cfg: dict, task: dict, tool_list: list | None,
        apply_policy: Callable[[dict, dict, list | None, bool], tuple],
) -> tuple | None:
    """Capture the ordinary policy, then re-apply it to the orchestration surface.

    ``apply_policy`` is the assembler's own ``_apply_assembled_tool_policy``;
    it has already run once for this task with the complete catalog. Returns
    the narrowed ``(tool_list, has_real_tools)`` or ``None`` when the mode
    cannot take effect because local Swarm is not in the catalog (headless
    caller-supplied tools, or a role that never receives spawn_agents). The
    caller then keeps the ordinary surface.
    """
    catalog = task.get('_executable_tool_catalog')
    if not isinstance(catalog, list):
        return None
    names = {_tool_name(tool) for tool in catalog}
    if 'spawn_agents' not in names:
        logger.warning(
            '[Task %s] Delegate mode requested but spawn_agents is not in the '
            'executable catalog; keeping the ordinary tool surface',
            str(task.get('id') or ''))
        return None
    task[DELEGATION_VIEW_KEY] = {
        'task': {key: _copy_value(task[key]) for key in _TASK_POLICY_FIELDS if key in task},
        'config': {key: _copy_value(value) for key, value in cfg.items()
                   if _is_policy_config_field(key, value)},
    }
    allowed = names & DELEGATE_ROOT_TOOLS
    admitted = [tool for tool in catalog if _tool_name(tool) in allowed]
    for key, value in list(cfg.items()):
        if key.endswith('ByName') and isinstance(value, dict):
            cfg[key] = {name: entry for name, entry in value.items() if name in allowed}
    cfg['_executableToolCatalog'] = admitted
    cfg['_requiredDirectToolNames'] = sorted(allowed)
    cfg['_toolDiscoveryPolicyByName'] = {name: 'eager' for name in allowed}
    cfg['_toolSearchCatalogSize'] = len(admitted)
    cfg['_toolSearchableCount'] = 0
    cfg['_frontendSelectedToolNames'] = sorted(
        set(cfg.get('_frontendSelectedToolNames') or ()) & allowed)
    cfg.pop('_toolInitialVisibleNames', None)
    # The first pass published its own maps on the task; the narrowed pass
    # reads cfg, so absent cfg maps must not let the broad task copies survive.
    for key in ('_toolContractDocumentsByName', '_executableToolNamespaceByName',
                '_toolScriptSafeByName', '_executableToolSearchTextByName',
                '_toolNamespaceByName'):
        cfg.setdefault(key, {name: entry for name, entry in (task.get(key) or {}).items()
                             if name in allowed})
    visible = [tool for tool in (tool_list or []) if _tool_name(tool) in allowed]
    return apply_policy(cfg, task, visible, bool(admitted))


def delegation_policy_view(task: dict) -> dict:
    """Return the tool policy that workers delegated by ``task`` inherit.

    Outside Delegate Mode this is ``task`` itself. Otherwise a shallow copy
    overlays the captured pre-narrowing fields (identity, principal and
    lifecycle fields stay the task's own) and drops the capture key, so
    applying the view again downstream is a no-op. Callers that further narrow
    the policy (retained snapshots, restart receipts) must apply the view
    first, then narrow the copy.
    """
    captured = task.get(DELEGATION_VIEW_KEY) if isinstance(task, dict) else None
    if not isinstance(captured, dict) or not captured:
        return task
    view = dict(task)
    view.pop(DELEGATION_VIEW_KEY, None)
    view.update(captured.get('task') or {})
    view['config'] = {**(task.get('config') or {}), **(captured.get('config') or {})}
    return view


def delegation_config_view(task: dict, config: dict) -> dict:
    """Overlay the captured pre-narrowing policy on a caller-held ``config``.

    Swarm launch receives the root's request config separately from the task;
    the narrowed root fields there must not override the delegation view.
    """
    captured = task.get(DELEGATION_VIEW_KEY) if isinstance(task, dict) else None
    if not isinstance(captured, dict) or not captured:
        return config
    return {**(config or {}), **(captured.get('config') or {})}


def delegation_tool_catalog(task: dict, fallback: list | None = None) -> list:
    """The executable catalog delegated workers inherit from ``task``.

    An explicit empty catalog stays empty (explicit denial); a task without a
    catalog falls back to the caller's assembled list.
    """
    view = delegation_policy_view(task)
    if '_executable_tool_catalog' in view:
        return list(view.get('_executable_tool_catalog') or [])
    return list(fallback or [])


def delegable_tool_names(task: Mapping | None) -> frozenset[str]:
    """Names workers delegated by a Delegate Mode root may receive.

    Empty outside Delegate Mode; capability guidance for the root (credential
    names, MCP hints) keys on this instead of the narrowed root catalog.
    """
    if not delegate_mode_active(task):
        return frozenset()
    return frozenset(_tool_name(tool) for tool in delegation_tool_catalog(dict(task)))


def delegate_call_rejection(task: Mapping, name: str, arguments: Any) -> str | None:
    """Return a model-visible denial before approval, hooks, or execution."""
    if not delegate_mode_active(task):
        return None
    if name in DELEGATE_ROOT_TOOLS and name != 'execute_tools':
        return None
    if name == 'execute_tools':
        args = arguments if isinstance(arguments, Mapping) else {}
        calls = args.get('calls')
        if (isinstance(calls, list) and calls and not args.get('program')
                and all(isinstance(call, Mapping)
                        and call.get('name') in DELEGATE_ROOT_TOOLS
                        and call.get('name') not in {'execute_tools', 'search_tools'}
                        for call in calls)):
            return None
    return (
        f'Tool call rejected: {name} is not available in Delegate mode. The root '
        'agent only orchestrates: launch workers with spawn_agents (they hold the '
        'full tool authority), wait with poll on their ids, and answer from their '
        'results. Put this work in a worker objective instead.')


def delegate_mode_prompt_block() -> str:
    """The Delegate Mode behavioural contract, injected as a context block."""
    return """<delegate_mode>
You are in **Delegate Mode**: you orchestrate, workers execute. You own the
logic flow — understand the request, decompose it, delegate, check results,
and answer. Workers launched with spawn_agents do every read, search,
command, web lookup and edit. Your own tools are limited to orchestration;
any other call is rejected.

## Delegation contract
- When a skill matches the request, load it first and reuse its templates
  verbatim: a fixed procedure keeps every run comparable.
- Workers never see this conversation. Give each a self-contained objective:
  goal and why, exact inputs (paths, ids, URLs, constraints), what done
  means, and the evidence to return.
- Same-shape work uses ONE entry in `templates` plus per-agent `params`, so
  every worker gets an identical prompt that differs only in its parameter
  block. Never paraphrase one instruction per worker.
- Ask for structured results with `output_schema` and put the evidence
  inside it (file:line, commands and exit codes, URLs). Keep schemas small:
  return what the next decision needs; bulky material goes to an artifact.
- Launch independent work in one spawn_agents call; use depends_on only for
  real prerequisites. While workers run, end your turn or wait with
  poll(ids=[...]); results arrive automatically.
- Verify before you report: for a surprising, high-impact or unproven
  result, spawn an independent reviewer with the claim and its evidence.
- Answer only from worker results. State failures, gaps and unverified
  claims plainly; never fill them in yourself.
</delegate_mode>"""

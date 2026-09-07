"""lib/tasks_pkg/plan_mode.py — Plan Mode: read-only collaborative planning.

Inspired by Codex's collaboration-mode ``plan.md`` (codex-rs). Plan Mode is one
choice in the interaction-mode radio (``cfg['planMode']``, camelCase like every
other wire key), while remaining orthogonal to the chat/studio capability dial.
Planning WITH a project's read-only tools attached is the primary use case.

Three enforcement layers (defense in depth; dispatch is the final authority):

  1. **Prompt** — a ``<plan_mode>`` context block (context_composer provider)
     teaches the model the read-only contract + the ``<proposed_plan>``
     protocol. Guidance only; never trusted as the enforcement.
  2. **Assembly** — ``model_config._assemble_tool_list`` drops mutating tool
     schemas from the wire when Plan Mode is on, so the model is not tempted
     (and tokens are saved). Best-effort guidance, NOT the authority.
  3. **Dispatch** — ``tool_dispatch/_pipeline`` rejects any call that is not
     positively proven read-only
     with a model-visible error (the same lane shape as the native
     multi-agent read-only guard). This is the enforcement authority: even a
     schema that leaked onto the wire (Tool Search, MCP discovered late,
     caller-supplied ``tools=[...]``) cannot mutate state.

The classifier starts with the per-task WRITE partition, adds the explicit
extra bans below, restricts mixed-effect desktop tools by their arguments, and
fails closed for unknown/caller-defined tools. MCP calls require an explicit
``readOnlyHint: true`` safety declaration.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from typing import Any

from lib.log import get_logger
from lib.plan_contract import extract_proposed_plan

logger = get_logger(__name__)

__all__ = [
    'PLAN_MODE_EXTRA_BAN',
    'plan_mode_enabled',
    'plan_mode_banned_names',
    'plan_mode_call_allowed',
    'plan_mode_filter_tool_schemas',
    'plan_mode_rejection',
    'extract_proposed_plan',

    'interaction_mode_generated_turn_identity',
    'normalize_interaction_mode_conversation_settings',
    'normalize_interaction_mode_runtime_config',
    'normalize_plan_mode_conversation_settings',
    'normalize_plan_mode_runtime_config',
    'plan_mode_prompt_block',
]

# ── Mutating tools that live OUTSIDE the write partition ──
# The write partition (tool_dispatch._flags) covers state-changing EXECUTION
# tools. These mutate state through other lanes and must also close in Plan
# Mode:
#   * todo_write        — Codex bans its update_plan checklist tool in Plan
#                         Mode for the same reason: the deliverable is ONE
#                         <proposed_plan> block, not a mutated checklist.
#   * spawn_agents      — a sub-agent runs its own task whose cfg may not
#                         carry planMode; rather than rely on propagation,
#                         close the lane (exploration is done inline).
#   * schedule_/timer_  — mutate the scheduler store.
#   * generate_image / produce_* — paid artifact generation (writes files,
#                         spends quota) — an execution, not an exploration.
#   * integration_*     — execution controls for an isolated writer.
PLAN_MODE_EXTRA_BAN = frozenset({
    'todo_write',
    'spawn_agents',
    'store_artifact',
    'schedule_create', 'schedule_manage',
    'timer_create', 'timer_manage',
    'generate_image',
    'produce_video', 'produce_report', 'produce_research',
    'produce_slides', 'edit_slides',
    'integration_checkpoint', 'integration_submit',
    # GUI automation always changes the user's interactive desktop state.
    'desktop_gui_action',
})


def plan_mode_enabled(cfg: dict | None) -> bool:
    """Whether this request declared Plan Mode (``planMode: true``).

    Strict bool read — the frontend ships a real boolean; anything else
    (absent/None/garbage) means OFF, matching the other atomic flags.
    """
    return (cfg or {}).get('planMode') is True


def _runtime_flow_selected(config: Mapping) -> bool:
    active_flow = config.get('activeFlow')
    return (
        isinstance(active_flow, str) and bool(active_flow.strip())
    ) or any(bool(config.get(key)) for key in (
        'flowDefinition', 'flowBuiltin', 'flowId'))


def normalize_interaction_mode_runtime_config(
    cfg: Mapping | None,
) -> dict:
    """Return one executable config with exactly one loop owner.

    This is the backend half of the Standard / Plan / Goal-mode (autopilot)
    radio surface. Plan wins stale conflicts fail-closed; otherwise an
    explicit Flow wins, then goal mode. The same precedence governs task
    dispatch.
    """
    result = dict(cfg or {})
    # Retired loop flags are consumed at this request boundary and never
    # propagated into executable configuration.
    result.pop('endpointMode', None)
    result.pop('endpointEnabled', None)
    if plan_mode_enabled(result):
        result['humanGuidanceEnabled'] = True
        result['autopilot'] = False
        result['imageGenMode'] = False
        result['activeFlow'] = ''
        result['autopilotEnabled'] = False
        for key in ('flowDefinition', 'flowBuiltin', 'flowId'):
            result.pop(key, None)
        return result

    if _runtime_flow_selected(result):
        result['autopilot'] = False
        result['autopilotEnabled'] = False
        result['imageGenMode'] = False
    elif (result.get('autopilot') is True
          or result.get('autopilotEnabled') is True):
        result['imageGenMode'] = False
    return result



def interaction_mode_generated_turn_identity(
    cfg: Mapping | None,
) -> tuple[str, str]:
    """Derive durable generated-Turn identity from normalized mode state."""
    normalized = normalize_interaction_mode_runtime_config(cfg)
    if plan_mode_enabled(normalized):
        return 'planner', 'plan'
    if _runtime_flow_selected(normalized):
        return 'assistant', 'flow_node'
    return 'assistant', 'reply'

def normalize_interaction_mode_conversation_settings(
    settings: Mapping | None,
) -> dict:
    """Return persisted settings with the same single-owner precedence."""
    result = dict(settings or {})
    result.pop('endpointEnabled', None)
    if plan_mode_enabled(result):
        result['humanGuidanceEnabled'] = True
        result['autopilotEnabled'] = False
        result['imageGenMode'] = False
        result['activeFlow'] = ''
        return result

    active_flow = result.get('activeFlow')
    if isinstance(active_flow, str) and active_flow.strip():
        result['autopilotEnabled'] = False
        result['imageGenMode'] = False
    elif result.get('autopilotEnabled') is True:
        result['imageGenMode'] = False
    return result


def normalize_plan_mode_runtime_config(cfg: Mapping | None) -> dict:
    """Compatibility name; normalize the complete interaction-mode state."""
    return normalize_interaction_mode_runtime_config(cfg)


def normalize_plan_mode_conversation_settings(settings: Mapping | None) -> dict:
    """Compatibility name; normalize the complete interaction-mode state."""
    return normalize_interaction_mode_conversation_settings(settings)


def plan_mode_banned_names(write_tools) -> frozenset:
    """Plan Mode ban set = the task's write partition UNION the extra bans.

    Pass the per-task write partition (``_task_partitions(task)[0]``) at
    dispatch time, or the registry-wide union at assembly time.
    """
    return frozenset(set(write_tools or ()) | set(PLAN_MODE_EXTRA_BAN))


def _declared_tool_names() -> set[str]:
    names: set[str] = set()
    try:
        from lib.tools.registry import all_specs
        for spec in all_specs():
            names.update(str(name) for name in spec.provides if name)
    except Exception as exc:
        logger.debug('[PlanMode] declared tool lookup failed: %s', exc)
    return names


def _mcp_read_only_names() -> set[str]:
    try:
        from lib.mcp import get_bridge
        bridge = get_bridge()
        if bridge.connected:
            return {
                str(name) for name, read_only in bridge.get_tool_safety().items()
                if read_only is True
            }
    except Exception as exc:
        logger.debug('[PlanMode] MCP read-only lookup failed: %s', exc)
    return set()


def plan_mode_call_allowed(fn_name: str, fn_args: Any, write_tools=()) -> bool:
    """Whether this exact call is proven non-mutating in Plan Mode.

    Unknown/caller-defined tools fail closed. Mixed-effect desktop tools are
    classified by arguments at the final dispatch boundary; this closes the
    name-only gap without unnecessarily banning their read branches.
    """
    name = str(fn_name or '')
    args = fn_args if isinstance(fn_args, Mapping) else {}
    if not name or name.startswith('custom__'):
        return False
    if name == 'desktop_clipboard':
        return args.get('action') == 'read'
    if name == 'desktop_system_info':
        return args.get('type') in {'overview', 'processes'}
    if name in plan_mode_banned_names(write_tools):
        return False
    return name in (_declared_tool_names() | _mcp_read_only_names())


def _readonly_variant(schema: Mapping[str, Any], name: str
                      ) -> dict[str, Any] | None:
    """Return a copied schema containing only a mixed tool's read branches."""
    if name == 'desktop_gui_action':
        return None
    if name not in {'desktop_clipboard', 'desktop_system_info'}:
        return copy.deepcopy(dict(schema))
    cloned = copy.deepcopy(dict(schema))
    function = cloned.get('function')
    if not isinstance(function, dict):
        return None
    parameters = function.get('parameters')
    properties = parameters.get('properties') if isinstance(parameters, dict) else None
    if not isinstance(properties, dict):
        return None
    discriminator = 'action' if name == 'desktop_clipboard' else 'type'
    field = properties.get(discriminator)
    if not isinstance(field, dict):
        return None
    field['enum'] = (['read'] if name == 'desktop_clipboard'
                     else ['overview', 'processes'])
    return cloned


def plan_mode_filter_tool_schemas(
    tools: list[dict] | None, write_tools=(),
) -> tuple[list[dict], list[str]]:
    """Filter/restrict a provider tool list using the dispatch authority."""
    known = _declared_tool_names() | _mcp_read_only_names()
    banned = plan_mode_banned_names(write_tools)
    kept: list[dict] = []
    dropped: list[str] = []
    for raw in tools or []:
        if not isinstance(raw, Mapping):
            continue
        fn = raw.get('function')
        name = str((fn.get('name') if isinstance(fn, Mapping) else '')
                   or raw.get('name') or '')
        if (not name or name.startswith('custom__') or name not in known
                or name in banned):
            if name:
                dropped.append(name)
            continue
        restricted = _readonly_variant(raw, name)
        if restricted is None:
            dropped.append(name)
            continue
        kept.append(restricted)
    return kept, dropped


def plan_mode_rejection(fn_name: str) -> str:
    """Model-visible error returned when a banned tool is called in Plan Mode.

    Mirrors Codex's plan.rs rejection tone: name the rule, name the way out.
    Kept deliberately specific so the model self-corrects on the next round
    instead of retrying the same call.
    """
    return (
        f'Tool call rejected: {fn_name} is not proven read-only and is not '
        'available in Plan mode. Plan mode is read-only: explore with read/search '
        'tools, ask the user questions, and finish with exactly one '
        '<proposed_plan> block. The user explicitly accepts the plan through '
        'one of the execution choices outside this tool call.'
    )


def plan_mode_prompt_block() -> str:
    """The Plan Mode behavioural contract, injected as a context block.

    Adapted from Codex's collaboration-mode-templates/templates/plan.md:
    three phases (silent exploration → intent clarification → proposed plan),
    the read-only contract, and the single-<proposed_plan> output protocol.
    """
    return """<plan_mode>
You are in **Plan Mode**. It ends ONLY when the user explicitly turns it off —
never because your plan looks complete, and never because the user's message
sounds imperative. A request to "do it" while Plan Mode is on is a request to
PLAN the work, not to perform it.

## Read-only contract
Plan Mode is strictly non-mutating. Tools that write files, run commands,
change settings, create memories/todos/scheduled tasks, spawn agents, drive
the browser/desktop, or generate artifacts are DISABLED — calling one returns
an error. You may read files, search, fetch, list, inspect, and ask the user.

## Workflow
1. **Explore first.** Ground yourself with read-only tools before asking
   anything. Never ask a question the codebase or docs can answer.
2. **Clarify intent.** Ask only about real decisions the user must make. When
   a human-guidance / multiple-choice tool is available and a decision has
   2-4 meaningful options, prefer presenting those options over open-ended
   questions.
3. **Propose.** When you have enough information, end your reply with exactly
   ONE <proposed_plan> block (opening and closing tags on their own lines),
   structured as: Summary → Key Changes (files/subsystems, in execution
   order) → Test Plan → Assumptions & Open Questions. Do NOT ask "should I
   proceed?" — after the plan is ready, the decision bar offers **Continue
   discussing**, **Execute with current context**, and **Execute in fresh
   context**. Only choosing an execution action starts work and exits Plan
   Mode; completing a plan or continuing the discussion does not.

## Revisions
If the user asks for changes, emit a new <proposed_plan> that is a COMPLETE
replacement of the previous one. If the request lacks information you cannot
obtain read-only, state exactly what is missing instead of guessing.
</plan_mode>"""

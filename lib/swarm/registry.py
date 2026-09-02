"""lib/swarm/registry.py — Agent role definitions and model-tier resolution.

Each role defines:
  - system_prompt_suffix — injected into the sub-agent's system prompt
  - tools_hint — which tool categories this role prefers (list of names)
  - model_hint — 'light', 'standard', or 'heavy' (resolved dynamically)

Model tiers are derived from a single source-of-truth: the user's selected
model (the "parent model").  Call ``configure_model_tiers(user_model)`` once
at swarm startup; afterwards ``resolve_model_for_tier()`` returns concrete
model names without any hardcoded defaults.
"""

import threading
from typing import Any

from lib.agent_verdict import VU_ROLE_PROMPT as _VU_ROLE_PROMPT_SHARED
from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Model Tier System — Evidence-backed ecosystem selection
# ═══════════════════════════════════════════════════════════
#
# Role declarations keep the small light/standard/heavy vocabulary. Concrete
# model selection is delegated to lib.model_profiles, which considers ONLY
# configured models carrying strong enough evidence. A newly discovered name
# with no declaration or benchmark never auto-promotes itself.

_current_parent_model: str = ''
_resolved_tiers: dict[str, str] = {}
_tier_lock = threading.Lock()


def _detect_family(model: str) -> str:
    """Backward-compatible family query over the profile parser."""
    from lib.model_profiles import infer_model_family
    return infer_model_family(model)


def _derive_tiers(parent_model: str, *, provider_id: str = '',
                  providers: list | None = None) -> dict[str, str]:
    from lib.model_profiles import select_model_for_tier
    return {
        tier: select_model_for_tier(
            tier, parent_model=parent_model, provider_id=provider_id,
            providers=providers)
        for tier in ('light', 'standard', 'heavy')
    }


def configure_model_tiers(user_model: str) -> dict[str, str]:
    """Cache an inspectable snapshot; request-time calls still re-resolve.

    Catalogues and provider prices can refresh while a process is running, so
    this cache is compatibility/UI state, not the routing authority.
    """
    global _current_parent_model, _resolved_tiers
    with _tier_lock:
        _current_parent_model = user_model
        _resolved_tiers = _derive_tiers(user_model)
    logger.info('[Registry] Model tiers configured from %r → %s',
                user_model, _resolved_tiers)
    return dict(_resolved_tiers)


def resolve_model_for_tier(tier: str, parent_model: str = '', *,
                           role: str = '', provider_id: str = '',
                           providers: list | None = None) -> str:
    """Resolve a role tier against the live configured model ecosystem.

    ``provider_id`` is a hard boundary for BYO/provider-pinned swarms. Weak or
    absent profile evidence falls back to the parent instead of guessing.
    """
    parent = parent_model or _current_parent_model or ''
    if tier not in ('light', 'standard', 'heavy'):
        return parent
    from lib.model_profiles import select_model_for_tier
    return select_model_for_tier(
        tier, parent_model=parent, role=role, provider_id=provider_id,
        providers=providers)


# Backward-compatible property: read-only snapshot of the current tiers.
# Callers that imported ``MODEL_TIERS`` as a dict get a live-ish view
# (it updates whenever ``configure_model_tiers`` is called).

class _TierProxy(dict):
    """A dict subclass that always reflects the current ``_resolved_tiers``."""

    def __getitem__(self, key: str) -> str:
        return _resolved_tiers.get(key, _current_parent_model)

    def get(self, key: str, default: str = '') -> str:     # type: ignore[override]
        return _resolved_tiers.get(key, default)

    def __repr__(self) -> str:
        return f'MODEL_TIERS({_resolved_tiers!r})'

    def __contains__(self, key: object) -> bool:
        return key in _resolved_tiers

    def keys(self):
        return _resolved_tiers.keys()

    def values(self):
        return _resolved_tiers.values()

    def items(self):
        return _resolved_tiers.items()

    def __iter__(self):
        return iter(_resolved_tiers)

    def __len__(self) -> int:
        return len(_resolved_tiers)


MODEL_TIERS: dict[str, str] = _TierProxy()  # type: ignore[assignment]


# ═══════════════════════════════════════════════════════════
#  Agent Role Definitions
# ═══════════════════════════════════════════════════════════

AGENT_ROLES: dict[str, dict[str, Any]] = {
    'researcher': {
        'when_to_use': (
            'Open-ended research questions and information gathering — '
            'web searches, doc lookups, comparing libraries, surveying APIs. '
            'Choose this role when the answer requires reading multiple '
            'web pages or external sources.'
        ),
        'system_prompt_suffix': (
            'You are a research specialist. Focus on gathering, verifying, '
            'and synthesizing information from available sources. '
            'Use web_search and fetch_url tools effectively. '
            'Cite sources and highlight confidence levels.'
        ),
        'tools_hint': ['web_search', 'fetch_url', 'browser_research_page',
                       'browser_read_page', 'browser_list_tabs'],
        'model_hint': 'standard',
    },

    'coder': {
        'when_to_use': (
            'Multi-file code investigations or modifications — find usages of X, '
            'audit a refactor, write a unit test, run a command and report. '
            'Use coder when the task touches code in the project.'
        ),
        'system_prompt_suffix': (
            'You are a coding specialist. Focus on reading, writing, '
            'and modifying code. Use project tools (read_files, write_file, '
            'grep_search, run_command, edit_file) effectively. '
            'Follow existing code conventions. Test your changes.'
        ),
        'tools_hint': ['read_files', 'write_file', 'edit_file',
                       'grep_search', 'find_files', 'run_command'],
        'model_hint': 'heavy',      # code generation benefits from strong models
    },

    'analyst': {
        'when_to_use': (
            'Quantitative analysis of data already on disk — log parsing, '
            'metric extraction, finding patterns in CSV / JSON / structured '
            'output. Choose this when the answer is numbers / tables.'
        ),
        'system_prompt_suffix': (
            'You are a data analysis specialist. Focus on understanding '
            'data, finding patterns, and providing clear insights. '
            'When given data, provide quantitative analysis with numbers. '
            'Summarize findings concisely with key takeaways.'
        ),
        'tools_hint': ['read_files', 'grep_search', 'run_command'],
        'model_hint': 'standard',
    },

    'browser': {
        'when_to_use': (
            'Tasks that require interacting with already-open browser tabs '
            '— click buttons, fill forms, scrape JS-rendered pages, take '
            'screenshots. Use this when web_search / fetch_url cannot reach '
            'the content because it needs interaction.'
        ),
        'system_prompt_suffix': (
            'You are a browser automation specialist. Use browser tools '
            'to navigate, read, click, and extract information from web pages. '
            'The concrete browser tools available vary by the current client '
            'and permission scope: use only tools present in this run\'s tool '
            'schemas, and report a missing capability instead of emulating it '
            'with shell commands.'
        ),
        'tools_hint': ['browser_list_tabs', 'browser_read_page',
                       'browser_research_page',
                       'browser_devtools', 'browser_execute_js',
                       'browser_screenshot',
                       'browser_click', 'browser_type', 'browser_press_key',
                       'browser_menu_click', 'browser_fill_form',
                       'browser_navigate', 'browser_close_tab',
                       'browser_preview_page', 'fetch_url'],
        'model_hint': 'standard',
    },

    'reviewer': {
        'when_to_use': (
            'Get a fresh, independent read on code or design — security '
            'review, bug hunting, code-style audit. Choose this for "second '
            'opinion" tasks where you want eyes that have not seen your '
            'analysis. Outputs a concrete punch list.'
        ),
        'system_prompt_suffix': (
            'You are a code/content reviewer. Carefully analyze the given '
            'code or content for bugs, style issues, security concerns, '
            'and improvement opportunities. Be specific and actionable.'
        ),
        'tools_hint': ['read_files', 'grep_search', 'find_files', 'run_command'],
        'model_hint': 'heavy',      # review needs deep understanding
    },

    'writer': {
        'when_to_use': (
            'Compose a long-form document — release notes, README sections, '
            'design docs, migration guides — from raw inputs you already have. '
            'Choose this when the task is mostly prose generation.'
        ),
        'system_prompt_suffix': (
            'You are a technical writer. Focus on creating clear, '
            'well-structured documentation, summaries, and explanations. '
            'Use markdown formatting. Be concise but comprehensive.'
        ),
        'tools_hint': ['read_files', 'write_file', 'grep_search'],
        'model_hint': 'light',      # writing is less computation-heavy
    },

    'general': {
        'when_to_use': (
            'Mixed / unclear tasks where no single specialist role fits — '
            'a sub-task that needs a couple of different tool families '
            'together. Default fallback when in doubt.'
        ),
        'system_prompt_suffix': (
            'You are a versatile assistant. Accomplish the given task '
            'using whatever tools and approaches are most appropriate.'
        ),
        'tools_hint': [],            # all tools available
        'model_hint': 'standard',
    },

    # ── Flow-only roles ──
    # Planner/worker/critic nodes need stable role behavior instead of silently
    # falling back to ``general``. Empty tools_hint means all tools.
    'planner': {
        'when_to_use': (
            'Flow planning step — rewrite the user request into a '
            'structured brief + checklist + acceptance criteria for the worker.'
        ),
        'system_prompt_suffix': (
            'You are the PLANNER. Rewrite the request into a structured brief '
            'with a Goal, a concrete Checklist of steps, and Acceptance '
            'Criteria. Produce a plan the worker can execute directly; do not '
            'do the work yourself.'
        ),
        'tools_hint': [],
        'model_hint': 'heavy',
    },

    'worker': {
        'when_to_use': (
            'Flow execution step — carry out the planner\'s checklist '
            'with full tools, accumulating progress across loop iterations.'
        ),
        'system_prompt_suffix': (
            'You are the WORKER. Execute the plan against the checklist. Your '
            'FIRST tool call MUST be state-changing — act, do not merely '
            'analyze. Address any reviewer feedback directly and build on your '
            'previous attempt rather than restarting.'
        ),
        'tools_hint': [],
        'model_hint': 'heavy',
    },

    'critic': {
        'when_to_use': (
            'Flow review step — verify the worker output against the '
            'checklist and emit a structured verdict.'
        ),
        'system_prompt_suffix': (
            'You are the CRITIC. Review the worker output against the plan\'s '
            'checklist and acceptance criteria. Mark each item ✅ or ❌. End '
            'with exactly one verdict tag: [VERDICT: STOP] when all criteria '
            'are met, [VERDICT: CONTINUE_WORKER] when the worker must keep '
            'going, or [PLAN_DEFECT: <reason>] + [VERDICT: CONTINUE_PLANNER] '
            'only for a genuine structural plan flaw (not worker execution).'
        ),
        'tools_hint': ['read_files', 'grep_search', 'find_files', 'run_command'],
        'model_hint': 'heavy',
    },

    # ── Autopilot role (used by the FlowExecutor autopilot path) ──
    # The virtual user stands in for the human: it auto-replies to keep the
    # task moving and signals completion with [VU: TASK_DONE]. Mirrors
    # lib/tasks_pkg/autopilot._VU_ROLE_PROMPT. Empty tools_hint = all tools
    # (the VU has the SAME tools as the worker, per the autopilot design).
    'virtual_user': {
        'when_to_use': (
            'Autopilot step — a synthetic user that auto-replies at every '
            'natural stop to keep a task progressing without a real human, '
            'until the assistant has clearly finished.'
        ),
        # SINGLE SOURCE: the VU persona is defined once in
        # lib.agent_verdict.VU_ROLE_PROMPT and shared with the live standalone
        # autopilot loop (lib/tasks_pkg/autopilot._VU_ROLE_PROMPT). This used
        # to be a hand-copied 3-sentence paraphrase that had drifted from the
        # ~2000-char original (verification discipline + the mandatory
        # [PROGRESS: resolved=X remaining=Y] hard-signal line were lost);
        # importing the shared constant kills that drift permanently.
        # tests/test_vu_prompt_single_source.py pins the identity.
        'system_prompt_suffix': _VU_ROLE_PROMPT_SHARED,
        'tools_hint': [],
        'model_hint': 'standard',
    },
}


# ═══════════════════════════════════════════════════════════
#  Public API — Role Queries
# ═══════════════════════════════════════════════════════════

def get_role_config(role: str) -> dict[str, Any]:
    """Get the full configuration dict for *role*.

    Falls back to ``'general'`` for unrecognised roles.
    """
    if role not in AGENT_ROLES:
        logger.warning('Unknown agent role: %r — falling back to general', role)
    return AGENT_ROLES.get(role, AGENT_ROLES['general'])


# Roles that exist for FlowExecutor/autopilot paths but are NOT
# meant to be spawned manually via ``spawn_agents``. They carry prompts that
# only make sense inside their host loop (a lone ``virtual_user`` or ``critic``
# sub-agent has nothing to drive). ``get_role_config`` still resolves them for
# flow runtime; they are excluded ONLY from the manual-spawn catalogue so the
# ``role`` param and the catalogue advertise the same 7 spawnable roles.
_CATALOGUE_EXCLUDED_ROLES = frozenset({
    'planner', 'worker', 'critic', 'virtual_user',
})


def format_role_catalogue() -> str:
    """Return a multi-line "role: when_to_use + tools" listing for prompt injection.

    This is what the master LLM reads in the ``spawn_agents`` tool
    description.  Mirrors Claude Code's ``Available agent types and the
    tools they have access to:`` block in ``AgentTool/prompt.ts`` —
    without an explicit role catalogue the model has no idea which
    role to pick and either falls back to ``general`` or doesn't spawn
    at all.

    Each role line ALSO carries its tool list (from ``tools_hint``),
    because a "when to use" blurb alone let the master hand a
    ``get_conversation`` task to ``researcher`` — which physically
    cannot run it (2026-07-27 incident).  Roles with an empty hint are
    unrestricted, so the catalogue spells that out as ``ALL tools minus
    <denylist>`` instead of leaving it ambiguous.  The tool names are
    derived from the same constants the executor enforces
    (:data:`AGENT_ROLES`, ``SUB_AGENT_DENYLIST``, ``ARTIFACT_TOOLS``) —
    never a hand-copied second list.

    Only MANUALLY-SPAWNABLE roles are listed; flow/autopilot-internal
    roles (see :data:`_CATALOGUE_EXCLUDED_ROLES`) are omitted so the catalogue
    matches the ``role`` param's advertised set.
    """
    # Local import — registry is loaded before tools.py finishes (circular
    # import avoidance), same pattern as scope_tools_for_role below.
    from lib.swarm.tools import ARTIFACT_TOOLS, SUB_AGENT_DENYLIST

    deny_str = '/'.join(sorted(SUB_AGENT_DENYLIST))
    lines = []
    for role, cfg in AGENT_ROLES.items():
        if role in _CATALOGUE_EXCLUDED_ROLES:
            continue
        when = cfg.get('when_to_use', '').strip().replace('\n', ' ')
        hint = cfg.get('tools_hint') or []
        tools_str = (', '.join(hint) if hint
                     else f'ALL tools minus {deny_str}')
        lines.append(f'  - {role}: {when} [tools: {tools_str}]')
    artifact_names = ', '.join(t['function']['name'] for t in ARTIFACT_TOOLS)
    lines.append(f'  Every role additionally receives the shared artifact '
                 f'tools: {artifact_names}.')
    return '\n'.join(lines)


def get_role_system_suffix(role: str) -> str:
    """Get the system prompt suffix for a role."""
    return get_role_config(role).get('system_prompt_suffix', '')


def get_role_model_hint(role: str) -> str:
    """Get the model tier hint for a role (``'light'`` / ``'standard'`` / ``'heavy'``)."""
    return get_role_config(role).get('model_hint', 'standard')


def get_tools_for_role(role: str) -> list[str]:
    """Get tool name hints for a role (list of strings, not full schemas).

    Useful for filtering which tools a sub-agent should have access to.
    """
    return get_role_config(role).get('tools_hint', [])


def scope_tools_for_role(role: str, all_tools: list) -> list:
    """Filter a full tool list to only those appropriate for *role*.

    Two filters are applied:

      1. **Role-specific allow-list** — tools whose ``function.name`` appears
         in the role's ``tools_hint``.  When the hint is empty (e.g.
         ``general``), all tools pass this filter.  A partial/empty match stays
         partial/empty: capability gates are authority boundaries, never a
         reason to expand a specialist to unrelated privileged tools.
      2. **Sub-agent deny-list** — swarm-control tools (``spawn_agents``,
         ``await_agents``, ``get_agent_result``) and ``ask_human`` are ALWAYS
         stripped, regardless of role.  Sub-agents must not be able to spawn
         further sub-agents or block on user interaction.

    Args:
        role: Agent role name (e.g. ``'coder'``, ``'researcher'``).
        all_tools: Full list of tool dicts (OpenAI function-calling schema).

    Returns:
        Filtered list of tool dicts.
    """
    # Local import — registry is loaded before tools.py finishes (circular
    # import avoidance). Loading here is cheap (tools.py is just constants).
    from lib.swarm.tools import SUB_AGENT_DENYLIST

    hints = get_tools_for_role(role)

    if hints:
        hint_set = set(hints)
        scoped = [
            tool for tool in all_tools
            if isinstance(tool, dict)
            and tool.get('function', {}).get('name', '') in hint_set
        ]
    else:
        scoped = list(all_tools)  # general role → all tools

    # Always strip sub-agent denylist (swarm-control + ask_human).
    return [
        tool for tool in scoped
        if not (isinstance(tool, dict)
                and tool.get('function', {}).get('name', '') in SUB_AGENT_DENYLIST)
    ]

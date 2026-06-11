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

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Model Tier System — Single Source-of-Truth
# ═══════════════════════════════════════════════════════════
#
# Tier semantics:
#   light    — fast / cheap.  Summaries, formatting, simple lookups.
#   standard — the parent model itself.  Default for most agents.
#   heavy    — strongest available.  Complex reasoning, code generation.
#
# The parent model is always "standard".  Light and heavy are derived
# from a known-family table when possible, otherwise they fall back
# to the parent model (safe: never picks an unknown model).

# Known model families — used to derive lighter / heavier variants
# when the user picks a model from a recognised family.
_MODEL_FAMILIES: dict[str, dict[str, str]] = {
    'gpt-4': {
        'light':    'gpt-4o-mini',
        'standard': 'gpt-4o',
        'heavy':    'gpt-4o',
    },
    'gpt-3.5': {
        'light':    'gpt-3.5-turbo',
        'standard': 'gpt-3.5-turbo',
        'heavy':    'gpt-4o',
    },
    # NOTE: Claude family intentionally omitted.
    # The API requires deployment-prefixed names (e.g. 'aws.claude-sonnet-4.6')
    # that vary per environment.  _derive_tiers() will use the parent model
    # (from the user's selection / CLAUDE_SONNET_MODEL) for all tiers, which
    # is already correct.  Add entries here only when light/heavy variants
    # with known API names become available.
    'qwen': {
        'light':    'qwen3-30b-a3b',
        'standard': 'qwen3-235b-a22b',
        'heavy':    'qwen3-235b-a22b',
    },
    'deepseek': {
        'light':    'deepseek-v4-flash',
        'standard': 'deepseek-v4-flash',
        'heavy':    'deepseek-v4-pro',
    },
    'gemini': {
        'light':    'gemini-2.0-flash',
        'standard': 'gemini-2.5-flash',
        'heavy':    'gemini-2.5-pro',
    },
}

# ── Runtime tier cache (populated by configure_model_tiers) ──────────

_current_parent_model: str = ''
_resolved_tiers: dict[str, str] = {}   # tier → model name
_tier_lock = threading.Lock()


def _detect_family(model: str) -> str:
    """Detect the model family from a model name string."""
    model_lower = model.lower()
    for family in _MODEL_FAMILIES:
        if family in model_lower:
            return family
    return ''


def _derive_tiers(parent_model: str) -> dict[str, str]:
    """Build a ``{tier: model_name}`` dict from *parent_model*.

    * ``standard`` is always *parent_model*.
    * ``light`` / ``heavy`` come from ``_MODEL_FAMILIES`` when the family
      is recognised; otherwise they fall back to *parent_model*.
    """
    tiers: dict[str, str] = {
        'light':    parent_model,
        'standard': parent_model,
        'heavy':    parent_model,
    }
    family = _detect_family(parent_model)
    if family and family in _MODEL_FAMILIES:
        family_map = _MODEL_FAMILIES[family]
        tiers['light'] = family_map.get('light', parent_model)
        tiers['heavy'] = family_map.get('heavy', parent_model)
        # standard is *always* the parent — don't override
    return tiers


def configure_model_tiers(user_model: str) -> dict[str, str]:
    """Set up the global tier cache from a single source-of-truth model.

    Call this once when the swarm session starts.  Subsequent calls to
    ``resolve_model_for_tier()`` (without an explicit *parent_model*) will
    use the cached mapping.

    Args:
        user_model: The model the user selected in the chat UI.

    Returns:
        The derived ``{tier: model_name}`` mapping (for inspection / logging).
    """
    global _current_parent_model, _resolved_tiers
    with _tier_lock:
        _current_parent_model = user_model
        _resolved_tiers = _derive_tiers(user_model)
    logger.info('[Registry] Model tiers configured from %r → %s',
                user_model, _resolved_tiers)
    return dict(_resolved_tiers)  # return a copy


def resolve_model_for_tier(tier: str, parent_model: str = '') -> str:
    """Resolve a tier hint to a concrete model name.

    Resolution strategy (in priority order):
      1. If *parent_model* is provided, derive tiers on the fly from it.
      2. Else use the cached tiers from ``configure_model_tiers()``.
      3. If nothing is configured, return ``''`` (caller should handle).

    Args:
        tier: ``'light'``, ``'standard'``, or ``'heavy'``.
        parent_model: Optional override; if given, tiers are derived from
            this model instead of the cached one.

    Returns:
        Resolved model name string.
    """
    if tier not in ('light', 'standard', 'heavy'):
        return parent_model or _current_parent_model or ''

    # If caller supplied an explicit parent, derive on the fly
    if parent_model:
        tiers = _derive_tiers(parent_model)
        resolved = tiers.get(tier, parent_model)
        logger.debug('[Registry] tier=%s parent=%s → %s (ad-hoc)',
                     tier, parent_model, resolved)
        return resolved

    # Use the cached mapping
    if _resolved_tiers:
        resolved = _resolved_tiers.get(tier, _current_parent_model)
        logger.debug('[Registry] tier=%s → %s (cached)', tier, resolved)
        return resolved

    # Nothing configured — return empty string
    logger.debug('[Registry] tier=%s → "" (no model configured)', tier)
    return ''


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
        'tools_hint': ['web_search', 'fetch_url', 'browser_read_tab',
                       'browser_list_tabs'],
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
            'grep_search, run_command, apply_diff) effectively. '
            'Follow existing code conventions. Test your changes.'
        ),
        'tools_hint': ['read_files', 'write_file', 'apply_diff', 'apply_diffs',
                       'insert_content', 'insert_contents',
                       'grep_search', 'find_files', 'list_dir', 'run_command'],
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
            'Use browser_list_tabs to find relevant tabs, browser_read_tab '
            'to extract content, and browser_execute_js for complex interactions.'
        ),
        'tools_hint': ['browser_list_tabs', 'browser_read_tab',
                       'browser_execute_js', 'browser_screenshot',
                       'browser_click', 'browser_navigate',
                       'browser_get_interactive_elements',
                       'browser_create_tab', 'browser_close_tab',
                       'fetch_url'],
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
        'tools_hint': ['read_files', 'grep_search', 'find_files', 'list_dir'],
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

    # ── Endpoint-mode roles (used by the FlowExecutor endpoint path) ──
    # These mirror lib/tasks_pkg/endpoint's planner/worker/critic prompts so
    # build_endpoint_definition's role nodes run with the right behavior
    # instead of silently falling back to 'general'. Empty tools_hint = all
    # tools (planner/worker/critic all need full tool access in endpoint mode).
    'planner': {
        'when_to_use': (
            'Endpoint-mode planning step — rewrite the user request into a '
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
            'Endpoint-mode execution step — carry out the planner\'s checklist '
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
            'Endpoint-mode review step — verify the worker output against the '
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
        'tools_hint': ['read_files', 'grep_search', 'find_files', 'list_dir'],
        'model_hint': 'heavy',
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


def format_role_catalogue() -> str:
    """Return a multi-line "role: when_to_use" listing for prompt injection.

    This is what the master LLM reads in the ``spawn_agents`` tool
    description.  Mirrors Claude Code's ``Available agent types and the
    tools they have access to:`` block in ``AgentTool/prompt.ts`` —
    without an explicit role catalogue the model has no idea which
    role to pick and either falls back to ``general`` or doesn't spawn
    at all.
    """
    lines = []
    for role, cfg in AGENT_ROLES.items():
        when = cfg.get('when_to_use', '').strip().replace('\n', ' ')
        lines.append(f'  - {role}: {when}')
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
         ``general``), all tools pass this filter.  A safety fallback expands
         to all tools if scoping produced fewer than 2 — keeps mis-configured
         roles from becoming useless.
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
        # Safety fallback: empty / near-empty scoping expands to all tools
        if len(scoped) < 2 and len(all_tools) > 2:
            scoped = list(all_tools)
    else:
        scoped = list(all_tools)  # general role → all tools

    # Always strip sub-agent denylist (swarm-control + ask_human).
    return [
        tool for tool in scoped
        if not (isinstance(tool, dict)
                and tool.get('function', {}).get('name', '') in SUB_AGENT_DENYLIST)
    ]

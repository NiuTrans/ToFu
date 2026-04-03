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
from typing import Any, Optional

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
        'light':    'deepseek-chat',
        'standard': 'deepseek-chat',
        'heavy':    'deepseek-reasoner',
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
    'planner': {
        'system_prompt_suffix': (
            'You are the planning specialist for a multi-agent swarm. '
            'Decompose complex tasks into clear, independent subtasks. '
            'For each subtask specify: role, objective, dependencies, and '
            'expected output format. Optimise for parallelism — minimise '
            'unnecessary sequential dependencies.'
        ),
        'tools_hint': [],           # planning uses no external tools
        'model_hint': 'heavy',      # planning benefits from strong reasoning
    },

    'researcher': {
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
        'system_prompt_suffix': (
            'You are a coding specialist. Focus on reading, writing, '
            'and modifying code. Use project tools (read_files, write_file, '
            'grep_search, run_command, apply_diff) effectively. '
            'Follow existing code conventions. Test your changes.'
        ),
        'tools_hint': ['read_files', 'write_file', 'apply_diff', 'grep_search',
                       'find_files', 'list_dir', 'run_command'],
        'model_hint': 'heavy',      # code generation benefits from strong models
    },

    'analyst': {
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
        'system_prompt_suffix': (
            'You are a code/content reviewer. Carefully analyze the given '
            'code or content for bugs, style issues, security concerns, '
            'and improvement opportunities. Be specific and actionable.'
        ),
        'tools_hint': ['read_files', 'grep_search', 'find_files', 'list_dir'],
        'model_hint': 'heavy',      # review needs deep understanding
    },

    'writer': {
        'system_prompt_suffix': (
            'You are a technical writer. Focus on creating clear, '
            'well-structured documentation, summaries, and explanations. '
            'Use markdown formatting. Be concise but comprehensive.'
        ),
        'tools_hint': ['read_files', 'write_file', 'grep_search'],
        'model_hint': 'light',      # writing is less computation-heavy
    },

    'general': {
        'system_prompt_suffix': (
            'You are a versatile assistant. Accomplish the given task '
            'using whatever tools and approaches are most appropriate.'
        ),
        'tools_hint': [],            # all tools available
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

    If the role has an empty ``tools_hint`` (e.g. ``general``), all tools
    are returned.  Otherwise, only tools whose ``function.name`` appears
    in the hint list are included.

    Args:
        role: Agent role name (e.g. ``'coder'``, ``'researcher'``).
        all_tools: Full list of tool dicts (OpenAI function-calling schema).

    Returns:
        Filtered list of tool dicts.
    """
    hints = get_tools_for_role(role)
    if not hints:
        return list(all_tools)  # general / planner → all tools

    hint_set = set(hints)
    scoped = [
        tool for tool in all_tools
        if isinstance(tool, dict)
        and tool.get('function', {}).get('name', '') in hint_set
    ]

    # Safety fallback: if scoping produced too few tools, include all
    if len(scoped) < 2 and len(all_tools) > 2:
        return list(all_tools)

    return scoped

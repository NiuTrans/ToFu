"""lib/tools/deferral.py — Tool deferral & search system.

Inspired by Claude Code's ``ToolSearchTool`` and ``shouldDefer`` mechanism.
Instead of loading ALL tools into the system prompt every turn (which costs
15-25K tokens for 25+ tools), rarely-used tools are marked as "deferred"
and only surfaced when the model calls ``tool_search`` to discover them.

Core concept:
  - Tools are partitioned into "core" (always loaded) and "deferred" (on demand)
  - A ``tool_search`` pseudo-tool lets the model discover deferred tools by keyword
  - Discovered tools are dynamically added to the tool list for the current task
  - Each deferred tool has a ``search_hint`` — keywords for fuzzy matching

Usage:
  1. Import ``partition_tools()`` in model_config.py
  2. Call it when assembling the tool list
  3. The tool_search tool is auto-added when deferred tools exist
"""

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Tool metadata: which tools are deferred + their search hints
# ═══════════════════════════════════════════════════════════════════════════════

# Mapping of tool_name → search_hint keywords.
# Used ONLY for Phase 2 dynamic deferral (when tool tokens exceed threshold).
# Phase 1 static deferral was REMOVED: all tools the user explicitly enabled
# via frontend toggles are included without delay. The _assemble_tool_list()
# function already gates tools by user toggles (browserEnabled, schedulerEnabled,
# imageGenEnabled, desktopEnabled, etc.), so every tool in the list is user-
# selected and should not be silently deferred.
#
# The hints are still useful for tool_search keyword matching when Phase 2
# dynamically defers tools under token pressure.
DEFERRED_TOOL_HINTS: dict[str, str] = {
    # Browser tools
    'browser_type':         'browser type input text field form keyboard',
    'browser_scroll':       'browser scroll page up down',
    'browser_select_option': 'browser select option dropdown menu choice',
    'browser_switch_tab':   'browser switch tab window',
    'browser_get_app_state': 'browser app state tabs url',
    'browser_read_tab':     'browser read tab page content text',
    'browser_screenshot':   'browser screenshot capture page visual',
    'browser_execute_js':   'browser execute javascript code eval',
    'browser_click':        'browser click element button link',
    'browser_navigate':     'browser navigate url go open page',
    'browser_get_interactive_elements': 'browser interactive elements form input',

    # Image generation
    'generate_image':       'image generate create picture draw illustration diagram',

    # Scheduler tools
    'create_scheduled_task': 'schedule task cron timer periodic automatic recurring',
    'list_scheduled_tasks':  'schedule list tasks cron show pending',
    'cancel_scheduled_task': 'schedule cancel delete remove task cron',

    # Desktop agent tools
    'desktop_screenshot':    'desktop screenshot capture screen monitor display',
    'desktop_click':         'desktop click mouse cursor button',
    'desktop_type':          'desktop type keyboard text input',
    'desktop_move_mouse':    'desktop mouse move cursor position',

    # Skills tools
    'create_skill':          'skill create save accumulate knowledge',
    'update_skill':          'skill update modify edit',
    'delete_skill':          'skill delete remove',
    'merge_skills':          'skill merge combine consolidate',

    # Error tracker tools
    'check_error_logs':      'error log check scan bugs issues',
    'resolve_error':         'error resolve fix mark resolved',

    # Swarm tools
    'spawn_agents':          'swarm spawn agents parallel multi-agent',
    'check_agents':          'swarm check agents status results',
}

# Core tools that are ALWAYS loaded (never deferred)
CORE_TOOL_NAMES = frozenset({
    # Project tools — fundamental for code work
    'read_files', 'list_dir', 'grep_search', 'find_files',
    'write_file', 'apply_diff', 'run_command',
    # Search & fetch
    'web_search', 'fetch_url',
    # Skills
    'create_skill', 'update_skill', 'delete_skill', 'merge_skills',
    # Error tracking
    'check_error_logs', 'resolve_error',
    # Local file reading (images, PDFs, Office docs)
    'read_local_file',
    # Essential meta
    'emit_to_user', 'ask_human',
    # Swarm
    'spawn_agents', 'check_agents',
    # Code execution
    'code_exec',
    # Conversation
    'read_conversation', 'search_conversations',
})


# ═══════════════════════════════════════════════════════════════════════════════
#  Tool search pseudo-tool definition
# ═══════════════════════════════════════════════════════════════════════════════

TOOL_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "tool_search",
        "description": (
            "Search for additional tools that aren't in your current tool list. "
            "Some specialized tools (browser interaction, image generation, scheduling, "
            "desktop control) are not loaded by default to save context. "
            "Use this when you need a capability that isn't available in your current tools.\n\n"
            "Returns matching tool names and descriptions. After discovery, "
            "the tools become available for use in subsequent calls."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "Keywords describing the capability you need. "
                        "E.g. 'browser click', 'generate image', 'schedule task', 'desktop screenshot'"
                    )
                }
            },
            "required": ["query"]
        }
    }
}


# ═══════════════════════════════════════════════════════════════════════════════
#  Partition and search logic
# ═══════════════════════════════════════════════════════════════════════════════

def _estimate_tool_tokens(tool_list: list) -> int:
    """Estimate the total token count for tool definitions.

    Each tool definition's JSON schema contributes to the context window.
    Rough heuristic: serialize to JSON, count chars, divide by 4.
    """
    import json
    try:
        total = len(json.dumps(tool_list, ensure_ascii=False))
        return total // 4
    except (TypeError, ValueError) as e:
        logger.debug('[Deferral] JSON serialization of tool list failed, using str fallback: %s', e)
        return len(str(tool_list)) // 4


# Context-aware deferral threshold — inspired by Claude Code's tst-auto mode.
# When tool definition tokens exceed this percentage of context window,
# consider deferring more tools dynamically.
_AUTO_DEFER_THRESHOLD_PCT = 10  # 10% of context window


def partition_tools(tool_list: list,
                    context_window: int = 200_000) -> tuple[list, list]:
    """Split a tool list into core (always loaded) and deferred (on-demand) tools.

    Enhanced with dynamic threshold-based deferral inspired by Claude Code's
    ``tst-auto`` mode: when total tool definition tokens exceed
    ``_AUTO_DEFER_THRESHOLD_PCT`` of the context window, additional
    non-essential tools are deferred even if they aren't in the static
    DEFERRED_TOOL_HINTS list.

    Args:
        tool_list: Full list of tool definitions (OpenAI function format).
        context_window: Model's context window in tokens (for dynamic threshold).

    Returns:
        (core_tools, deferred_tools): Two lists. Deferred tools are NOT
        included in the API call but are available via tool_search.
    """
    if not tool_list:
        return [], []

    core = list(tool_list)  # start with ALL user-selected tools
    deferred = []

    # Phase 1: REMOVED.
    # All tools in tool_list are user-selected via frontend toggles
    # (browserEnabled, schedulerEnabled, imageGenEnabled, etc.).
    # User-selected tools must NEVER be silently deferred.
    # Previously, tools in DEFERRED_TOOL_HINTS were auto-deferred here,
    # which violated user intent.

    # Phase 2: Dynamic threshold-based deferral (tst-auto inspired)
    # If core tools still exceed the token threshold, defer the largest
    # non-essential core tools (e.g. skills tools, error tracker)
    token_threshold = int(context_window * _AUTO_DEFER_THRESHOLD_PCT / 100)
    core_tokens = _estimate_tool_tokens(core)

    if core_tokens > token_threshold and len(core) > 5:
        # Sort non-essential core tools by definition size (largest first)
        _NEVER_DEFER = frozenset({
            'read_files', 'list_dir', 'grep_search', 'find_files',
            'write_file', 'apply_diff', 'run_command',
            'web_search', 'fetch_url', 'emit_to_user',
        })
        deferrable = []
        kept = []
        for td in core:
            fn_name = td.get('function', {}).get('name', '')
            if fn_name in _NEVER_DEFER:
                kept.append(td)
            else:
                deferrable.append(td)

        # Defer largest tools first until under threshold
        import json
        deferrable.sort(
            key=lambda t: len(json.dumps(t, ensure_ascii=False)),
            reverse=True,
        )
        while deferrable and _estimate_tool_tokens(kept + deferrable) > token_threshold:
            deferred_tool = deferrable.pop(0)
            deferred.append(deferred_tool)
            _def_name = deferred_tool.get('function', {}).get('name', '')
            logger.info('[ToolDeferral] Auto-deferred %s (dynamic threshold: '
                        'tools=%d tokens > %d threshold)',
                        _def_name, core_tokens, token_threshold)

        core = kept + deferrable

    if deferred:
        # Add tool_search to the core tools
        core.append(TOOL_SEARCH_TOOL)
        logger.info('[ToolDeferral] Partitioned %d tools: %d core + %d deferred '
                    '(deferred: %s)',
                    len(tool_list), len(core), len(deferred),
                    ', '.join(t.get('function', {}).get('name', '') for t in deferred))

    return core, deferred


def search_deferred_tools(query: str, deferred_tools: list) -> list:
    """Search deferred tools by keyword matching.

    Args:
        query: User's search query (keywords).
        deferred_tools: List of deferred tool definitions.

    Returns:
        List of matching tool definitions (to be added to the active tool list).
    """
    if not query or not deferred_tools:
        return []

    query_words = set(query.lower().split())
    results = []

    for tool_def in deferred_tools:
        fn_name = tool_def.get('function', {}).get('name', '')
        hint = DEFERRED_TOOL_HINTS.get(fn_name, '')
        hint_words = set(hint.lower().split())

        # Score by keyword overlap
        overlap = query_words & hint_words
        if overlap or any(qw in fn_name.lower() for qw in query_words):
            results.append(tool_def)

    if results:
        found_names = [t.get('function', {}).get('name', '') for t in results]
        logger.info('[ToolSearch] query=%r matched %d tools: %s',
                    query, len(results), ', '.join(found_names))
    else:
        logger.info('[ToolSearch] query=%r matched 0 deferred tools', query)

    return results


def format_search_results(matched_tools: list) -> str:
    """Format discovered tools into a human-readable response for the model.

    Args:
        matched_tools: Tool definitions that matched the search query.

    Returns:
        Formatted string describing the discovered tools.
    """
    if not matched_tools:
        return ("No matching tools found. Available deferred categories: "
                "browser (navigate, click, type, screenshot, read), "
                "image generation, scheduling, desktop control.")

    lines = [f"Found {len(matched_tools)} tool(s) — now available for use:\n"]
    for tool_def in matched_tools:
        fn = tool_def.get('function', {})
        name = fn.get('name', '?')
        desc = fn.get('description', '')
        # Truncate long descriptions
        if len(desc) > 200:
            desc = desc[:200] + '…'
        lines.append(f"  • **{name}**: {desc}")

    lines.append("\nThese tools are now available. You can call them directly.")
    return '\n'.join(lines)


__all__ = [
    'DEFERRED_TOOL_HINTS', 'CORE_TOOL_NAMES', 'TOOL_SEARCH_TOOL',
    'partition_tools', 'search_deferred_tools', 'format_search_results',
]

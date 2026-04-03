"""Tool-call display helpers — build search-round entries and tool_start events.

Extracted from ``orchestrator.py`` to keep the main run-loop module focused on
orchestration logic.  The public entry-point is :func:`_build_tool_round_entry`;
the per-tool ``_tool_display_*`` helpers are internal to this module.
"""

from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.scheduler import SCHEDULER_TOOL_NAMES
from lib.skills import SKILL_TOOL_NAMES
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES
from lib.tools import (
    BROWSER_TOOL_NAMES,
    CODE_EXEC_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    EMIT_TO_USER_TOOL_NAMES,
    ERROR_TRACKER_TOOL_NAMES,
    IMAGE_GEN_TOOL_NAMES,
    PROJECT_TOOL_NAMES,
)

# ── Tool round entry dispatch ─────────────────────────────────────────
#  Instead of a massive if/elif chain, we use a dispatch dict pattern.
#  Each handler returns (display_str, extra_fields_dict).


def _tool_display_web_search(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for web_search tool calls."""
    query = fn_args.get('query', '')
    return query, {'toolName': 'web_search'}


def _tool_display_fetch_url(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for fetch_url tool calls."""
    target_url = fn_args.get('url', '')
    domain = urlparse(target_url).netloc
    is_pdf_hint = target_url.lower().rstrip('/').endswith('.pdf')
    display_query = f'{"📑 PDF" if is_pdf_hint else "🌐"} {domain}'
    return f'📄 {target_url}', {'toolName': 'fetch_url', '_display_query': display_query}


def _tool_display_code_exec(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for standalone code execution tool calls."""
    from lib.project_mod import project_tool_display
    display = project_tool_display(fn_name, fn_args)
    return display, {'toolName': 'code_exec'}


def _tool_display_project(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for project tool calls."""
    from lib.project_mod import project_tool_display
    display = project_tool_display(fn_name, fn_args)
    return display, {'toolName': fn_name}


def _tool_display_browser(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for browser tool calls (basic + advanced)."""
    from lib.browser import browser_tool_display
    display = browser_tool_display(fn_name, fn_args)
    return display, {'toolName': fn_name}


def _tool_display_skill(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for skill management tool calls."""
    if fn_name == 'create_skill':
        display = f"💡 Saving skill: {fn_args.get('name', '?')}"
    elif fn_name == 'update_skill':
        display = f"✏️ Updating skill: {fn_args.get('skill_id', '?')}"
    elif fn_name == 'delete_skill':
        display = f"🗑️ Deleting skill: {fn_args.get('skill_id', '?')}"
    elif fn_name == 'merge_skills':
        ids = fn_args.get('skill_ids', [])
        display = f"🔀 Merging {len(ids)} skills → {fn_args.get('name', '?')}"
    else:
        display = f"💡 {fn_name}"
    return display, {'toolName': fn_name}


def _tool_display_conv_ref(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for conversation reference tool calls."""
    icon = '📋' if fn_name == 'list_conversations' else '💬'
    kw = fn_args.get('keyword', 'all') if fn_name == 'list_conversations' else fn_args.get('conversation_id', '?')[:8]
    display = f"{icon} {fn_name}: {kw}"
    return display, {'toolName': fn_name}


def _tool_display_scheduler(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for scheduler tool calls."""
    return f"⏰ {fn_name}", {'toolName': fn_name}


def _tool_display_desktop(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for desktop tool calls."""
    return f"🖥️ {fn_name}", {'toolName': fn_name}


def _tool_display_swarm(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for swarm tool calls."""
    if fn_name == 'spawn_agents':
        n_agents = len(fn_args.get('agents', [])) if isinstance(fn_args, dict) else 0
        display = f"Spawning {n_agents} agent{'s' if n_agents != 1 else ''}…" if n_agents else "Spawning agents…"
    else:
        display = fn_name.replace('_', ' ').title()
    return display, {'toolName': fn_name, '_swarm': True}


def _tool_display_compact(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for context_compact tool calls."""
    return '🗜️ Compacting context…', {'toolName': fn_name}


def _tool_display_image_gen(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for image generation tool calls."""
    prompt = fn_args.get('prompt', '…')[:80]
    return f'🎨 Generating: {prompt}', {'toolName': 'generate_image'}


def _tool_display_error_tracker(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for error tracker tool calls."""
    if fn_name == 'check_error_logs':
        mode = fn_args.get('mode', 'unresolved')
        filt = fn_args.get('logger_filter', '')
        display = f"🔍 Checking error logs ({mode})" + (f" filter={filt}" if filt else "")
    elif fn_name == 'resolve_error':
        fp = fn_args.get('fingerprint', '')
        fps = fn_args.get('fingerprints', [])
        lg = fn_args.get('logger_name', '')
        pat = fn_args.get('message_pattern', '')
        if fp:
            display = f"✅ Resolving error: {fp}"
        elif fps:
            display = f"✅ Resolving {len(fps)} errors"
        elif lg:
            display = f"✅ Resolving errors from {lg}"
        elif pat:
            display = f"✅ Resolving errors matching /{pat}/"
        else:
            display = "✅ resolve_error"
    else:
        display = f"🔍 {fn_name}"
    return display, {'toolName': fn_name}


def _tool_display_human_guidance(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for ask_human tool calls."""
    question = fn_args.get('question', '…')[:80]
    response_type = fn_args.get('response_type', 'free_text')
    icon = '🗳️' if response_type == 'choice' else '🙋'
    return f'{icon} {question}', {'toolName': 'ask_human'}


def _tool_display_emit_to_user(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for emit_to_user tool calls."""
    comment = fn_args.get('comment', '…')[:80]
    tool_round = fn_args.get('tool_round', '?')
    return f'📤 Emit result (round {tool_round}): {comment}', {'toolName': 'emit_to_user'}


def _tool_display_tool_search(fn_name, fn_args, tc_id, tc_args_str):
    """Build display info for tool_search (deferred tool discovery) calls."""
    query = fn_args.get('query', '…')[:80]
    return f'🔍 Searching tools: {query}', {'toolName': 'tool_search'}


def _tool_display_generic(fn_name, fn_args, tc_id, tc_args_str):
    """Catch-all display info for unknown/future tools."""
    logger.warning('[Orchestrator] Unregistered tool %s — using generic round_entry. This tool may need a dedicated display handler.', fn_name)
    return f"🔧 {fn_name}", {'toolName': fn_name}


# ══════════════════════════════════════════════════════════════════════
#  Module-level dispatch table (hoisted from _build_tool_round_entry)
# ══════════════════════════════════════════════════════════════════════
# This dict is built once at module load time instead of being rebuilt on
# every call.  The only runtime-dynamic part is CODE_EXEC_TOOL_NAMES
# which depends on the ``project_enabled`` flag — that is handled inside
# _build_tool_round_entry with a cheap conditional override.

def _build_display_dispatch_table():
    """Build the static tool-name → handler dispatch table.

    Called once at module load time.  Returns the dict.
    """
    table = {}

    # Direct name matches
    table['web_search'] = _tool_display_web_search
    table['fetch_url'] = _tool_display_fetch_url
    table['context_compact'] = _tool_display_compact

    # Code exec tools — default to project handler (overridden at call
    # time when project is disabled).
    for name in CODE_EXEC_TOOL_NAMES:
        table.setdefault(name, _tool_display_project)

    # Project tools
    for name in PROJECT_TOOL_NAMES:
        table.setdefault(name, _tool_display_project)

    # Browser tools (basic + advanced)
    for name in BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser
    for name in ADVANCED_BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser

    # Skill tools
    for name in SKILL_TOOL_NAMES:
        table[name] = _tool_display_skill

    # Conversation reference tools
    for name in CONV_REF_TOOL_NAMES:
        table[name] = _tool_display_conv_ref

    # Scheduler tools
    for name in SCHEDULER_TOOL_NAMES:
        table[name] = _tool_display_scheduler

    # Desktop tools
    for name in DESKTOP_TOOL_NAMES:
        table[name] = _tool_display_desktop

    # Swarm tools
    for name in SWARM_TOOL_NAMES:
        table[name] = _tool_display_swarm

    # Image generation tools
    for name in IMAGE_GEN_TOOL_NAMES:
        table[name] = _tool_display_image_gen

    # Error tracker tools
    for name in ERROR_TRACKER_TOOL_NAMES:
        table[name] = _tool_display_error_tracker

    # Human guidance tool
    table['ask_human'] = _tool_display_human_guidance

    # Emit-to-user terminal tool
    for name in EMIT_TO_USER_TOOL_NAMES:
        table[name] = _tool_display_emit_to_user

    # Deferred tool discovery
    table['tool_search'] = _tool_display_tool_search

    return table


# Hoisted constant — built once at import time.
_TOOL_DISPLAY_DISPATCH = _build_display_dispatch_table()


def _build_tool_round_entry(fn_name, fn_args, tc_id, tc_args_str, search_round_num,
                             project_enabled):
    """Build a search-round entry and tool_start event payload for a tool call.

    Uses a module-level dispatch table (``_TOOL_DISPLAY_DISPATCH``) instead of
    rebuilding a dict on every call.  The only runtime override is for
    CODE_EXEC_TOOL_NAMES when ``project_enabled`` is False — those get
    redirected to ``_tool_display_code_exec``.

    Returns (new_search_round_num, round_entry, event_payload).
    """
    # ── Runtime override: code-exec tools display differently when project
    #    mode is off (standalone code execution vs. project tool).
    if not project_enabled and fn_name in CODE_EXEC_TOOL_NAMES:
        handler = _tool_display_code_exec
    else:
        handler = _TOOL_DISPLAY_DISPATCH.get(fn_name, _tool_display_generic)

    try:
        display_query, extra = handler(fn_name, fn_args, tc_id, tc_args_str)
    except Exception as e:
        logger.warning('[ToolDisplay] handler for %s raised: %s', fn_name, e)
        display_query = f'🔧 {fn_name}'
        extra = {'toolName': fn_name}

    search_round_num += 1
    rn = search_round_num

    # Build round_entry
    round_entry = {
        'roundNum': rn,
        'query': display_query,
        'results': None,
        'status': 'searching',
        'toolCallId': tc_id,
        'toolArgs': tc_args_str,
    }
    round_entry.update(extra)

    # Build tool_start event — same fields + type
    event = {
        'type': 'tool_start',
        'roundNum': rn,
        'query': extra.get('_display_query', display_query),
        'toolCallId': tc_id,
        'toolArgs': tc_args_str,
    }
    # Copy relevant extra fields into event (toolName, _swarm, etc.)
    for k, v in extra.items():
        if not k.startswith('_display_'):
            event[k] = v

    return search_round_num, round_entry, event

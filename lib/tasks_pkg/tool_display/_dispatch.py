"""Tool-round dispatch: the static tool-name → renderer table plus the two
public entry points ``tool_round_label`` (side-effect-free label) and
``_build_tool_round_entry`` (round-entry + tool_start event payload).

Instead of a massive if/elif chain we use a dispatch dict pattern; each
handler returns ``(display_str, extra_fields_dict)``.
"""

from lib.log import get_logger

logger = get_logger(__name__)

from lib.browser.advanced import ADVANCED_BROWSER_TOOL_NAMES
from lib.desktop_tools import DESKTOP_TOOL_NAMES
from lib.scheduler import SCHEDULER_TOOL_NAMES
from lib.memory import MEMORY_TOOL_NAMES
from lib.tasks_pkg.executor import SWARM_TOOL_NAMES
from lib.tools import (
    BOARD_TOOL_NAMES,
    BROWSER_TOOL_NAMES,
    CHARTER_TOOL_NAMES,
    CODE_EXEC_TOOL_NAMES,
    CONV_REF_TOOL_NAMES,
    IMAGE_EDIT_TOOL_NAMES,
    IMAGE_GEN_TOOL_NAMES,
    PEER_TOOL_NAMES,
    PROJECT_TOOL_NAMES,
)

from lib.tasks_pkg.tool_display._renderers import (
    _tool_display_brain,
    _tool_display_browser,
    _tool_display_code_exec,
    _tool_display_compact,
    _tool_display_conv_ref,
    _tool_display_desktop,
    _tool_display_fetch_url,
    _tool_display_generic,
    _tool_display_human_guidance,
    _tool_display_image_gen,
    _tool_display_inspect_image,
    _tool_display_mcp,
    _tool_display_memory,
    _tool_display_project,
    _tool_display_scheduler,
    _tool_display_swarm,
    _tool_display_todo,
    _tool_display_web_search,
)
from lib.tasks_pkg.tool_display._roots import _resolve_tool_root_name


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

    # ★ read_files — global tool (not in PROJECT_TOOL_NAMES), uses same
    #   project-style display rendering (path + line ranges; icon is the
    #   frontend SVG, no emoji prefix).
    table.setdefault('read_files', _tool_display_project)

    # Browser tools (basic + advanced)
    for name in BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser
    for name in ADVANCED_BROWSER_TOOL_NAMES:
        table[name] = _tool_display_browser

    # Memory tools
    for name in MEMORY_TOOL_NAMES:
        table[name] = _tool_display_memory

    # Conversation reference tools
    for name in CONV_REF_TOOL_NAMES:
        table[name] = _tool_display_conv_ref

    # Project-brain tools (board / charter / peer / feed) — friendly collapsed
    # label + no spurious "unregistered tool" WARNING on every call.
    for name in (BOARD_TOOL_NAMES | CHARTER_TOOL_NAMES | PEER_TOOL_NAMES):
        table[name] = _tool_display_brain

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

    # Image inspection tool (zoom/rotate/crop viewer)
    for name in IMAGE_EDIT_TOOL_NAMES:
        table[name] = _tool_display_inspect_image

    # Human guidance tool
    table['ask_human'] = _tool_display_human_guidance

    # Structured task-checklist tool (todo_write) — friendly progress label,
    # no spurious "unregistered tool" WARNING on every checklist update.
    table['todo_write'] = _tool_display_todo

    return table


# Hoisted constant — built once at import time.
_TOOL_DISPLAY_DISPATCH = _build_display_dispatch_table()


def tool_round_label(fn_name, fn_args):
    """Return the human-readable tool-round label chat would render for a call.

    Public, side-effect-free entry point over the same ``_tool_display_*``
    dispatch table the chat orchestrator uses, so secondary agent surfaces
    (paper report / Q&A) get IDENTICAL, string/dict-safe labels — including
    the multi-line batch rendering (``N searches:\\n• …``) and the empty-list
    guards — instead of reimplementing them. Prefers the richer
    ``_display_query`` (multi-line) over the compact form when the handler
    supplies one.

    Args:
        fn_name: Tool name.
        fn_args: The DECODED + repaired arguments dict (run it through
            ``lib.tool_input_repair.parse_and_repair_tool_args`` first).

    Returns:
        The display string. Falls back to the tool name on any handler error.
    """
    handler = _TOOL_DISPLAY_DISPATCH.get(fn_name, _tool_display_generic)
    try:
        display_query, extra = handler(fn_name, fn_args, '', '')
    except Exception as e:
        logger.warning('[ToolDisplay] tool_round_label handler for %s raised: %s', fn_name, e)
        return fn_name
    return extra.get('_display_query', display_query)


def _build_tool_round_entry(fn_name, fn_args, tc_id, tc_args_str, tool_round_num,
                             project_enabled, conv_id=None):
    """Build a tool-round entry and tool_start event payload for a tool call.

    Uses a module-level dispatch table (``_TOOL_DISPLAY_DISPATCH``) instead of
    rebuilding a dict on every call.  The only runtime override is for
    CODE_EXEC_TOOL_NAMES when ``project_enabled`` is False — those get
    redirected to ``_tool_display_code_exec``.

    When ``conv_id`` is supplied and the tool is a filesystem tool in a
    multi-root workspace, attaches ``_toolRoot`` to both the round entry
    and the SSE event so the frontend can render a ``rootname:`` pill.

    Returns (new_tool_round_num, round_entry, event_payload).
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
        display_query = fn_name
        extra = {'toolName': fn_name}

    tool_round_num += 1
    rn = tool_round_num

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

    # ── Multi-root workspace pill: attach the workspace-root name the
    #    tool call resolves to, so the frontend can render a
    #    ``rootname:`` prefix on the tool-call line. Only meaningful for
    #    filesystem tools, and only when more than one root is registered.
    try:
        root_name = _resolve_tool_root_name(fn_name, fn_args, conv_id=conv_id)
    except Exception as e:
        logger.debug('[ToolDisplay] _resolve_tool_root_name failed for %s: %s',
                     fn_name, e)
        root_name = ''
    if root_name:
        round_entry['_toolRoot'] = root_name
        event['_toolRoot'] = root_name

    return tool_round_num, round_entry, event

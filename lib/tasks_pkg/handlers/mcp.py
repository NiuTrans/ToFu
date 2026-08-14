# HOT_PATH
"""MCP tool handler — dispatches tool calls to MCP servers via the bridge.

Unlike other handlers that register for specific tool names, MCP tools are
dynamic: their names are only known at runtime after connecting to MCP servers.
The handler is registered as a **fallback** on the ToolRegistry that catches
any ``mcp__*`` prefixed tool name.

Registration pattern:
  - We don't use @tool_registry.handler() because MCP tool names are dynamic.
  - Instead, we extend ToolRegistry.lookup() to fall back to the MCP handler
    for any tool name starting with ``mcp__``.
"""

from __future__ import annotations

import types as _types
from typing import Any

from lib.log import get_logger
from lib.mcp.types import MCP_TOOL_PREFIX
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call

logger = get_logger(__name__)


def _run_progressive_mcp(fn_name, fn_args):
    """Executor callable for the stable progressive MCP meta tools."""
    from lib.mcp import get_bridge
    from lib.mcp.progressive import (
        MCP_SEARCH_TOOL_NAME,
        call_progressive_mcp,
        search_mcp_catalog,
    )

    bridge = get_bridge()
    try:
        if fn_name == MCP_SEARCH_TOOL_NAME:
            return search_mcp_catalog(
                bridge,
                fn_args.get('query', ''),
                server=fn_args.get('server', ''),
                limit=fn_args.get('limit', 5),
            )
        return call_progressive_mcp(
            bridge, fn_name, fn_args.get('name', ''),
            fn_args.get('arguments', {}),
        )
    except Exception as e:
        logger.error('[MCP:Progressive] %s failed: %s', fn_name, e,
                     exc_info=True)
        return f'MCP tool error: {e}'


@tool_registry.handler(
    {'search_mcp_tools', 'call_mcp_read_tool', 'call_mcp_write_tool'},
    category='mcp', description='Progressive MCP discovery and invocation')
def handle_progressive_mcp_tool(
    task: dict[str, Any],
    tc: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    all_tools: list[dict] | None = None,
) -> tuple[str, str, bool]:
    """Execute one progressive MCP search/read/write wrapper call."""
    from lib.mcp.progressive import MCP_SEARCH_TOOL_NAME

    underlying = str(fn_args.get('name') or '')

    def _post_build(meta, _tool_content, _fn_args):
        if fn_name == MCP_SEARCH_TOOL_NAME:
            meta['badge'] = 'MCP catalog'
            meta['title'] = f"Search MCP tools: {str(fn_args.get('query') or '')[:120]}"
            return
        try:
            from lib.mcp import get_bridge
            from lib.tasks_pkg.tool_display import compose_mcp_display
            info = get_bridge().get_tool_info(underlying)
            meta['badge'] = info['server_name'] if info else 'MCP'
            display, _ = compose_mcp_display(
                underlying, fn_args.get('arguments') or {})
            meta['title'] = display or underlying or fn_name
        except Exception as e:
            logger.debug('[MCP:Progressive] display build failed for %s: %s',
                         underlying, e)

    result = simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_progressive_mcp,
        source='MCP', module_tag='MCP:Progressive',
        extra={'mcpTool': underlying, 'progressive': True},
        post_build=_post_build,
    )
    if fn_name == MCP_SEARCH_TOOL_NAME:
        misses = 1
        try:
            import json
            payload = json.loads(result[0]) if isinstance(result[0], str) else {}
            misses = int(not bool(payload.get('matches')))
        except Exception as e:
            logger.debug('[MCP:Progressive] search result was not parseable '
                         'for telemetry: %s', e)
        try:
            from lib.context_telemetry import record_mcp_search
            record_mcp_search(task, misses=misses)
        except Exception as e:
            logger.debug('[MCP:Progressive] search telemetry failed: %s', e)
    return result


def _run_mcp(fn_name, fn_args):
    """Executor callable for simple_call — returns tool_content string."""
    from lib.mcp import get_bridge
    bridge = get_bridge()
    try:
        return bridge.call_tool(fn_name, fn_args)
    except Exception as e:
        logger.error('[MCP] %s failed: %s', fn_name, e, exc_info=True)
        return f'MCP tool error: {e}'


def handle_mcp_tool(
    task: dict[str, Any],
    tc: dict[str, Any],
    fn_name: str,
    tc_id: str,
    fn_args: dict[str, Any],
    rn: int,
    round_entry: dict[str, Any],
    cfg: dict[str, Any],
    project_path: str | None,
    project_enabled: bool,
    all_tools: list[dict] | None = None,
) -> tuple[str, str, bool]:
    """Handle an MCP tool call by dispatching to the MCP bridge.

    This handler is invoked by the ToolRegistry fallback for any tool name
    that starts with ``mcp__``.
    """
    # Look up server/tool display names before execution so meta is consistent
    from lib.mcp import get_bridge
    bridge = get_bridge()
    info = bridge.get_tool_info(fn_name)
    server_name = info['server_name'] if info else '?'
    tool_name = info['tool_name'] if info else fn_name

    # Surface the most informative arg (file_path, name, section_title,
    # short project_id, owner/repo, batch file paths, …) so the title shown
    # in the UI tells users *which resource* the call touches — instead of
    # every create_file / edit_file looking identical.
    #
    # ★ Single source of truth: use compose_mcp_display (the SAME helper the
    #   live tool-round line uses in tool_display._tool_display_mcp), NOT a
    #   direct _mcp_arg_suffix call — otherwise batch-file tools (batch_commit
    #   / push_files) whose paths live inside a list regress to a branch-only
    #   ``server/tool — main @ owner/repo`` title once execution completes.
    from lib.tasks_pkg.tool_display import compose_mcp_display
    base_display, _ = compose_mcp_display(fn_name, fn_args)

    def _post_build(meta, tool_content, _fn_args):
        """Upgrade badge/title with MCP server/tool pair + arg context.

        Also opportunistically learns any project_id → name mappings from
        overleaf tool results so future calls' title lines can show the
        human-readable project name (e.g. ``@ [EMNLP Demo] Tofu``)
        instead of an opaque ``@ 69f21…cca7``. Cache population is
        best-effort — errors are logged and swallowed.
        """
        # ── Opportunistic project-name learning ──
        try:
            from lib.mcp.project_names import ingest_tool_result
            ingest_tool_result(fn_name, _fn_args, tool_content)
        except Exception as e:  # noqa: BLE001
            logger.debug('[MCP] project-name ingest failed for %s: %s', fn_name, e)

        # ── Rebuild the display AFTER ingest so this very call can benefit
        #    from the name it just learned (e.g. the create_project call
        #    itself now renders as ``… — My Paper`` rather than a short-ID).
        #    Same compose_mcp_display helper as the live line → no drift, incl.
        #    the batch-file (one-path-per-line) form.
        from lib.tasks_pkg.tool_display import compose_mcp_display
        fresh_display, _ = compose_mcp_display(fn_name, fn_args)
        fresh_display = fresh_display or base_display

        is_error = isinstance(tool_content, str) and tool_content.startswith(
            ('❌', 'MCP Error', 'MCP tool error', 'MCP server not connected'))
        meta['badge'] = server_name if not is_error else f'{server_name} (error)'
        meta['title'] = fresh_display

    try:
        from lib.mcp.tool_search import record_mcp_tool_used
        record_mcp_tool_used(str(task.get('id') or ''), fn_name)
    except Exception as e:
        logger.debug('[MCP] sticky active-tool update failed for %s: %s',
                     fn_name, e)

    return simple_call(
        task, fn_name, fn_args, rn, round_entry, tc_id,
        executor=_run_mcp,
        source=f'MCP:{server_name}', module_tag='MCP',
        extra={'mcpServer': server_name, 'mcpTool': tool_name},
        post_build=_post_build,
    )


# ── Register the MCP fallback on the ToolRegistry ──
# We monkey-patch the lookup method to check for MCP tools before
# returning None.  This is cleaner than modifying ToolRegistry itself,
# as the MCP bridge is an optional feature.

_original_lookup = tool_registry.lookup.__func__


def _lookup_with_mcp_fallback(self, fn_name: str, round_entry=None):
    """Extended lookup: try normal registry first, then MCP fallback."""
    result = _original_lookup(self, fn_name, round_entry)
    if result is not None:
        return result

    # MCP fallback: check if this is an MCP tool
    if fn_name.startswith(MCP_TOOL_PREFIX):
        try:
            from lib.mcp import get_bridge
            bridge = get_bridge()
            if bridge.is_mcp_tool(fn_name):
                return handle_mcp_tool
        except Exception as e:
            logger.warning('[MCP] Fallback lookup failed for %s: %s', fn_name, e)

    return None


# Apply the patched lookup
tool_registry.lookup = _types.MethodType(_lookup_with_mcp_fallback, tool_registry)
logger.debug('[MCP] ToolRegistry.lookup patched with MCP fallback')

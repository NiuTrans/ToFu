# HOT_PATH
"""Per-task handler resolution seam — D1 of tool-dispatch isolation.

One named authority for the order every dispatch already used implicitly:
task-local handlers (``task['_tool_env']``, per-request custom tools) first,
the process built-in registry second. The registry is an explicit parameter
so the fallback becomes swappable — that parameter is where D2's immutable
built-in table attaches, and where the owner-scoped MCP layer (D3) will sit
between the two. Callers use :func:`resolver_for`; nothing here mutates
registration state, and a failing task-local env degrades to the fallback
exactly as the inline pattern did before.
"""
from __future__ import annotations

from typing import Any

from lib.log import get_logger
from lib.protocols import ToolHandler

logger = get_logger(__name__)


class HandlerResolver:
    """Resolve a tool name to its handler for one task.

    Resolution order is the isolation contract:

    1. task-local ``_tool_env`` (per-request custom tools; never global)
    2. the fallback registry (today: the process built-in singleton)
    """

    def __init__(self, task: dict[str, Any] | None, *, registry: Any = None,
                 env: Any = None) -> None:
        if registry is None:
            from lib.tasks_pkg.executor._registry import tool_registry
            registry = tool_registry
        self._registry = registry
        self._env = env if env is not None else (task or {}).get('_tool_env')
        try:
            self._owner_id = int((task or {}).get('_userId') or 0)
        except (TypeError, ValueError):
            self._owner_id = 0

    def lookup(self, fn_name: str,
               round_entry: dict[str, Any] | None = None,
               ) -> ToolHandler | None:
        if self._env is not None:
            handler = None
            try:
                handler = self._env.resolve(fn_name)
            except Exception as exc:
                logger.warning(
                    '[HandlerResolver] tool_env.resolve failed for %s: %s',
                    fn_name, exc)
            if handler is not None:
                return handler
        handler = self._registry.lookup(fn_name, round_entry)
        if handler is not None:
            return handler
        return self._mcp_fallback(fn_name)

    def _mcp_fallback(self, fn_name: str) -> ToolHandler | None:
        """Owner-scoped MCP fallback (D3): an ``mcp__`` name unknown to the
        registry resolves only through THIS task's owner's bridge — never
        through another owner's, and never through a monkey-patched global
        lookup (the anti-pattern this replaces)."""
        from lib.mcp.types import MCP_TOOL_PREFIX
        if not fn_name.startswith(MCP_TOOL_PREFIX) or self._owner_id < 1:
            return None
        try:
            from lib.mcp import get_bridge
            bridge = get_bridge(self._owner_id)
            if bridge.is_mcp_tool(fn_name):
                from lib.tasks_pkg.handlers.mcp import handle_mcp_tool
                return handle_mcp_tool
        except Exception as exc:
            logger.warning('[HandlerResolver] MCP fallback failed for %s: %s',
                           fn_name, exc)
        return None


def resolver_for(task: dict[str, Any] | None, **kwargs) -> HandlerResolver:
    """The one entry point dispatch paths call; keeps the seam mockable."""
    return HandlerResolver(task, **kwargs)

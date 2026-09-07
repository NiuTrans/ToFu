"""lib/mcp/ — MCP (Model Context Protocol) bridge package.

Bridges external MCP servers (e.g. ClawHub tools, community tools) into Tofu's
tool system.  MCP tools are translated to OpenAI function-calling format so the
LLM can discover and invoke them alongside native Tofu tools.

Sub-modules:
  client      — MCPBridge: manages MCP server lifecycles, tool discovery, call dispatch
  config      — Persistent config read/write for MCP server definitions
  types       — Shared type definitions and constants

Architecture:
  ClawHub MCP Tool (subprocess)
       ↕  stdio / SSE  (JSON-RPC 2.0)
  MCPBridge (lib/mcp/client.py)
       ↕  translate to OpenAI function-calling format
  ToolRegistry (lib/tasks_pkg/executor/_registry.py)
       ↕
  LLM (function-calling)

Usage::

    from lib.mcp import get_bridge

    bridge = get_bridge()
    bridge.connect_all()                    # connect to all configured servers
    tools = bridge.get_openai_tool_defs()   # → list of OpenAI tool dicts
    result = bridge.call_tool('mcp__github__list_issues', {'repo': 'org/repo'})
"""

from lib.mcp.client import MCPBridge, get_bridge

__all__ = ['MCPBridge', 'get_bridge']

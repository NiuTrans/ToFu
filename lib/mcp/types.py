"""lib/mcp/types.py — Shared constants and type definitions for MCP bridge."""

from __future__ import annotations

from typing import Any, TypedDict

# ── Namespace separator for MCP tool names ──
# MCP tools are exposed to the LLM as  mcp__{server}__{tool}
# Double-underscore avoids collision with single-underscore in tool names.
MCP_TOOL_PREFIX = 'mcp__'
MCP_TOOL_SEP = '__'

# ── Config file path (relative to project data/config/) ──
MCP_CONFIG_FILENAME = 'mcp_servers.json'

# ── Limits ──
MCP_CONNECT_TIMEOUT = 30        # seconds to wait for server handshake
MCP_CALL_TIMEOUT = 120          # seconds to wait for tool call response
MCP_MAX_RESULT_CHARS = 200_000  # truncate tool results beyond this


class MCPServerConfig(TypedDict, total=False):
    """Configuration for a single MCP server."""
    command: str                # executable (e.g. 'npx', 'python3', 'node')
    args: list[str]             # command-line arguments
    env: dict[str, str]         # extra environment variables (merged with os.environ)
    url: str                    # for SSE/HTTP transport (alternative to stdio)
    transport: str              # 'stdio' (default) or 'sse'
    enabled: bool               # whether to connect on startup (default: True)
    description: str            # human-readable description
    timeout: int                # per-call timeout override (seconds)


class MCPToolInfo(TypedDict):
    """Internal representation of a discovered MCP tool."""
    server_name: str
    tool_name: str
    namespaced_name: str        # mcp__{server}__{tool}
    description: str
    input_schema: dict[str, Any]
    openai_def: dict[str, Any]  # ready-to-use OpenAI function-calling dict


def make_namespaced_name(server_name: str, tool_name: str) -> str:
    """Build the namespaced tool name: ``mcp__{server}__{tool}``.

    Deduping safety net: if ``tool_name`` starts with ``{server_name}_``
    (e.g. server ``hope`` exposes ``hope_login``) we strip the redundant
    prefix so the LLM sees ``mcp__hope__login`` instead of the stuttering
    ``mcp__hope__hope_login``. The MCP protocol already namespaces by
    server, so repeating the server name in the tool name is just noise.
    """
    prefix = f'{server_name}_'
    if tool_name.startswith(prefix) and len(tool_name) > len(prefix):
        tool_name = tool_name[len(prefix):]
    return f'{MCP_TOOL_PREFIX}{server_name}{MCP_TOOL_SEP}{tool_name}'


def parse_namespaced_name(namespaced: str) -> tuple[str, str] | None:
    """Parse ``mcp__{server}__{tool}`` → ``(server_name, tool_name)``.

    Returns None if the name doesn't match the MCP pattern.
    """
    if not namespaced.startswith(MCP_TOOL_PREFIX):
        return None
    rest = namespaced[len(MCP_TOOL_PREFIX):]
    parts = rest.split(MCP_TOOL_SEP, 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return parts[0], parts[1]

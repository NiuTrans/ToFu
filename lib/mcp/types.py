"""lib/mcp/types.py — Shared constants and type definitions for MCP bridge."""

from __future__ import annotations

import os
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

# ── Auto-recovery / keepalive ──
# A background loop pings every connected server this often (seconds) and
# transparently reconnects any whose transport has died — so idle-dropped
# connections self-heal without user intervention. Set
# TOFU_MCP_KEEPALIVE_INTERVAL=0 to disable the proactive loop (the reactive
# reconnect-on-call path still applies). MCP_PING_TIMEOUT bounds each ping.
MCP_KEEPALIVE_INTERVAL = int(os.environ.get('TOFU_MCP_KEEPALIVE_INTERVAL', '45'))
MCP_PING_TIMEOUT = int(os.environ.get('TOFU_MCP_PING_TIMEOUT', '10'))

# ── Circuit breaker (stop hammering a permanently-broken server) ──
# Backoff applies only AFTER a reconnect attempt fails, so a transient drop
# still heals on the very next keepalive sweep. Once reconnects keep failing,
# the next attempt is gated by an exponentially growing delay:
#   delay = min(BASE * 2**(consecutive_failures - 1), MAX)
# A permanently-broken server therefore converges to one retry per MAX
# seconds (default 10 min) instead of every sweep — but never gives up, so it
# self-heals if the server eventually comes back. A successful reconnect (or
# manual reconnect) resets the breaker.
MCP_BREAKER_BASE_BACKOFF = int(os.environ.get('TOFU_MCP_BREAKER_BASE_BACKOFF', '30'))
MCP_BREAKER_MAX_BACKOFF = int(os.environ.get('TOFU_MCP_BREAKER_MAX_BACKOFF', '600'))

# ── Call-level health gating (stop paying full-timeout calls to a stalled server) ──
# The circuit breaker above only covers RECONNECT failures. A server whose
# transport is alive but whose calls keep timing out (e.g. a long-poll tool
# whose own budget exceeds MCP_CALL_TIMEOUT) would otherwise be hammered with
# back-to-back full-timeout calls forever. After this many CONSECUTIVE call
# timeouts a server is marked 'degraded' and the next call fast-fails with an
# actionable error instead of blocking for the full timeout again. Any single
# successful call resets the streak. Set to 0 to disable the gate.
MCP_DEGRADED_TIMEOUT_STREAK = int(os.environ.get('TOFU_MCP_DEGRADED_TIMEOUT_STREAK', '3'))

# ── Credential health probe (detect expired cookies/tokens quietly) ──
# Transport health (a live subprocess + a successful protocol ping) does NOT
# imply the server's stored CREDENTIALS are still valid — an Overleaf session
# cookie expires (~30 days) while the subprocess stays happily connected, so
# every real tool call returns an auth-error string. To surface that in the
# settings panel WITHOUT a user action, a server whose catalog entry declares a
# read-only ``health_probe`` tool is probed once on connect and then every
# MCP_CRED_PROBE_INTERVAL seconds by the keepalive loop; the probe result text
# is classified against the entry's ``fail_patterns`` into ok / expired. Set to
# 0 to disable the periodic probe (the connect-time probe still runs).
MCP_CRED_PROBE_INTERVAL = int(os.environ.get('TOFU_MCP_CRED_PROBE_INTERVAL', '900'))


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
    read_only_hint: bool        # MCP annotations.readOnlyHint (default False)


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

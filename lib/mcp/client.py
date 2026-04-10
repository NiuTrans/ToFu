"""lib/mcp/client.py — MCP Bridge: lifecycle, discovery, dispatch.

Manages MCP server subprocesses, discovers their tools, translates to OpenAI
function-calling format, and dispatches tool calls back to the correct server.

Design notes:
  - Each MCP server runs as a child process (stdio transport) or remote
    endpoint (SSE transport).
  - Sessions are long-lived and reused across task rounds.
  - A background asyncio event loop runs in a dedicated daemon thread so
    the sync Flask/tool-handler code can call ``bridge.call_tool()`` without
    blocking the main event loop.
  - Thread-safe: all public methods are safe to call from any thread.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Any

from lib.log import get_logger, log_context
from lib.mcp.config import load_mcp_config
from lib.mcp.types import (
    MCP_CALL_TIMEOUT,
    MCP_CONNECT_TIMEOUT,
    MCP_MAX_RESULT_CHARS,
    MCPToolInfo,
    make_namespaced_name,
    parse_namespaced_name,
)

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════
#  Async core — runs on a dedicated event loop thread
# ══════════════════════════════════════════════════════════

class _MCPServerHandle:
    """Internal handle for a connected MCP server."""

    __slots__ = ('name', 'config', 'session', 'tools', '_ctx_stack',
                 '_read', '_write', '_session_ctx')

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session = None       # mcp.ClientSession (set after connect)
        self.tools: list = []     # list of mcp.types.Tool
        self._ctx_stack = None    # AsyncExitStack managing context managers
        self._read = None
        self._write = None
        self._session_ctx = None


class MCPBridge:
    """Bridge between MCP servers and Tofu's tool system.

    Lifecycle:
        1. ``connect_all()`` — reads config, launches servers, discovers tools.
        2. ``get_openai_tool_defs()`` — returns translated tool definitions.
        3. ``call_tool(namespaced_name, args)`` — dispatches to correct server.
        4. ``disconnect_all()`` — gracefully shuts down all servers.

    Thread safety:
        All public methods are thread-safe.  Async operations run on an
        internal event loop managed by a daemon thread.
    """

    def __init__(self) -> None:
        self._servers: dict[str, _MCPServerHandle] = {}
        self._tool_index: dict[str, MCPToolInfo] = {}  # namespaced_name → info
        self._lock = threading.Lock()

        # Dedicated asyncio event loop for MCP sessions
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._started = False

    # ── Event loop management ─────────────────────────────

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        """Start the background event loop thread if not running."""
        if self._loop is not None and self._loop.is_running():
            return self._loop
        loop = asyncio.new_event_loop()
        self._loop = loop

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        t = threading.Thread(target=_run, name='mcp-event-loop', daemon=True)
        t.start()
        self._loop_thread = t
        # Wait for loop to be running
        for _ in range(50):
            if loop.is_running():
                break
            time.sleep(0.05)
        return loop

    def _run_async(self, coro) -> Any:
        """Run an async coroutine on the MCP event loop, blocking until done."""
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=MCP_CALL_TIMEOUT + 10)

    # ── Connection management ─────────────────────────────

    def connect_all(self) -> dict[str, list[str]]:
        """Connect to all enabled MCP servers defined in config.

        Returns:
            Dict mapping server_name → list of tool names discovered.
        """
        config = load_mcp_config()
        if not config:
            logger.info('[MCP] No MCP servers configured')
            return {}

        result = {}
        for name, srv_cfg in config.items():
            if not srv_cfg.get('enabled', True):
                logger.info('[MCP] Skipping disabled server: %s', name)
                continue
            try:
                tools = self.connect_server(name, srv_cfg)
                result[name] = [t.name for t in tools]
            except Exception as e:
                logger.error('[MCP] Failed to connect server %s: %s', name, e, exc_info=True)
        return result

    def connect_server(self, name: str, srv_cfg: dict) -> list:
        """Connect to a single MCP server and discover its tools.

        Args:
            name: Unique server identifier (used as namespace).
            srv_cfg: Server configuration dict.

        Returns:
            List of ``mcp.types.Tool`` objects discovered.
        """
        with self._lock:
            # Disconnect existing server with same name
            if name in self._servers:
                logger.info('[MCP] Reconnecting server %s (was already connected)', name)
                try:
                    self._run_async(self._async_disconnect_one(name))
                except Exception as e:
                    logger.warning('[MCP] Error disconnecting old %s: %s', name, e)

        with log_context(f'mcp_connect:{name}', logger=logger):
            tools = self._run_async(self._async_connect(name, srv_cfg))

        with self._lock:
            # Build tool index
            for tool in tools:
                ns_name = make_namespaced_name(name, tool.name)
                self._tool_index[ns_name] = MCPToolInfo(
                    server_name=name,
                    tool_name=tool.name,
                    namespaced_name=ns_name,
                    description=tool.description or '',
                    input_schema=tool.inputSchema or {'type': 'object', 'properties': {}},
                    openai_def=self._tool_to_openai(name, tool),
                )
            self._started = True

        logger.info('[MCP] Server %s connected — %d tools discovered: %s',
                    name, len(tools),
                    ', '.join(t.name for t in tools))
        return tools

    async def _async_connect(self, name: str, srv_cfg: dict) -> list:
        """Async: connect to one MCP server via stdio or SSE transport."""
        from contextlib import AsyncExitStack

        handle = _MCPServerHandle(name, srv_cfg)
        stack = AsyncExitStack()
        handle._ctx_stack = stack

        transport = srv_cfg.get('transport', 'stdio')

        if transport == 'sse':
            url = srv_cfg.get('url', '')
            if not url:
                raise ValueError(f'MCP server {name}: SSE transport requires "url"')
            from mcp.client.sse import sse_client
            read, write = await stack.enter_async_context(
                sse_client(url)
            )
        else:
            # stdio transport (default)
            command = srv_cfg.get('command', '')
            args = srv_cfg.get('args', [])
            if not command:
                raise ValueError(f'MCP server {name}: stdio transport requires "command"')

            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            # Merge env: os.environ + custom env vars
            env = dict(os.environ)
            # Node.js does NOT read HTTP_PROXY / HTTPS_PROXY by default.
            # Two mechanisms ensure proxy support for child processes:
            #   1. NODE_USE_ENV_PROXY=1 — env-var flag (Node ≥ v22.21)
            #   2. NODE_OPTIONS=--use-env-proxy — CLI flag via NODE_OPTIONS,
            #      which propagates to ALL child node processes (including
            #      those spawned by npx, which may not inherit env-var flags).
            env.setdefault('NODE_USE_ENV_PROXY', '1')
            existing_opts = env.get('NODE_OPTIONS', '')
            if '--use-env-proxy' not in existing_opts:
                env['NODE_OPTIONS'] = (
                    f'{existing_opts} --use-env-proxy'.strip()
                )
            extra_env = srv_cfg.get('env', {})
            if extra_env:
                env.update(extra_env)

            params = StdioServerParameters(
                command=command,
                args=args,
                env=env,
            )
            read, write = await stack.enter_async_context(
                stdio_client(params)
            )

        # Create and initialize session
        from mcp import ClientSession

        session = await stack.enter_async_context(
            ClientSession(read, write)
        )
        await asyncio.wait_for(session.initialize(), timeout=MCP_CONNECT_TIMEOUT)

        # Discover tools
        response = await asyncio.wait_for(session.list_tools(), timeout=MCP_CONNECT_TIMEOUT)

        handle.session = session
        handle.tools = response.tools
        handle._read = read
        handle._write = write

        with self._lock:
            self._servers[name] = handle

        return response.tools

    def disconnect_all(self) -> None:
        """Gracefully disconnect all MCP servers."""
        with self._lock:
            names = list(self._servers.keys())
        for name in names:
            try:
                self._run_async(self._async_disconnect_one(name))
                logger.info('[MCP] Disconnected server: %s', name)
            except Exception as e:
                logger.warning('[MCP] Error disconnecting %s: %s', name, e)
        with self._lock:
            self._servers.clear()
            self._tool_index.clear()
            self._started = False

        # Shut down the event loop
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._loop_thread:
                self._loop_thread.join(timeout=5)
            self._loop = None
            self._loop_thread = None
        logger.info('[MCP] All servers disconnected')

    async def _async_disconnect_one(self, name: str) -> None:
        """Async: disconnect a single server by cleaning up its context stack."""
        with self._lock:
            handle = self._servers.pop(name, None)
            # Remove tool index entries
            to_remove = [k for k, v in self._tool_index.items()
                         if v['server_name'] == name]
            for k in to_remove:
                del self._tool_index[k]

        if handle and handle._ctx_stack:
            try:
                await handle._ctx_stack.aclose()
            except Exception as e:
                # Cancel scope / task mismatch is expected when disconnect
                # is called from a different coroutine than the one that
                # opened the context managers.  The subprocess is still
                # killed — this is cosmetic.
                logger.debug('[MCP] Context cleanup for %s (non-critical): %s', name, e)

    # ── Tool translation ──────────────────────────────────

    @staticmethod
    def _tool_to_openai(server_name: str, tool) -> dict[str, Any]:
        """Translate an MCP Tool to OpenAI function-calling format.

        MCP tool schema::

            Tool(name='search', description='...', inputSchema={...})

        OpenAI tool schema::

            {"type": "function", "function": {"name": "mcp__tavily__search",
             "description": "[MCP:tavily] ...", "parameters": {...}}}
        """
        ns_name = make_namespaced_name(server_name, tool.name)
        desc = tool.description or f'MCP tool: {tool.name}'
        # Prefix description with server name for disambiguation
        tagged_desc = f'[MCP:{server_name}] {desc}'
        # Clean up the input schema: ensure it has required fields
        schema = dict(tool.inputSchema) if tool.inputSchema else {'type': 'object', 'properties': {}}
        if 'type' not in schema:
            schema['type'] = 'object'

        return {
            'type': 'function',
            'function': {
                'name': ns_name,
                'description': tagged_desc,
                'parameters': schema,
            },
        }

    # ── Tool discovery (for LLM) ──────────────────────────

    def get_openai_tool_defs(self) -> list[dict[str, Any]]:
        """Get all MCP tools as OpenAI function-calling definitions.

        Returns:
            List of OpenAI tool dicts ready to append to the tool_list.
        """
        with self._lock:
            return [info['openai_def'] for info in self._tool_index.values()]

    def get_tool_info(self, namespaced_name: str) -> MCPToolInfo | None:
        """Look up tool info by namespaced name."""
        with self._lock:
            return self._tool_index.get(namespaced_name)

    def is_mcp_tool(self, fn_name: str) -> bool:
        """Check if a function name is a registered MCP tool."""
        with self._lock:
            return fn_name in self._tool_index

    @property
    def server_count(self) -> int:
        with self._lock:
            return len(self._servers)

    @property
    def tool_count(self) -> int:
        with self._lock:
            return len(self._tool_index)

    @property
    def connected(self) -> bool:
        return self._started and self.server_count > 0

    def list_servers(self) -> list[dict[str, Any]]:
        """List all connected servers with their tool counts.

        Returns:
            List of dicts with keys: name, tools_count, tool_names, description.
        """
        with self._lock:
            result = []
            for name, handle in self._servers.items():
                result.append({
                    'name': name,
                    'tools_count': len(handle.tools),
                    'tool_names': [t.name for t in handle.tools],
                    'description': handle.config.get('description', ''),
                    'transport': handle.config.get('transport', 'stdio'),
                })
            return result

    # ── Tool execution ────────────────────────────────────

    def call_tool(self, namespaced_name: str, arguments: dict[str, Any]) -> str:
        """Execute an MCP tool call and return the result as a string.

        Args:
            namespaced_name: Full namespaced tool name (``mcp__{server}__{tool}``).
            arguments: Tool arguments dict.

        Returns:
            Tool result as a string (text content extracted from MCP response).

        Raises:
            ValueError: If the tool or server is not found.
            TimeoutError: If the call exceeds the timeout.
        """
        parsed = parse_namespaced_name(namespaced_name)
        if parsed is None:
            raise ValueError(f'Invalid MCP tool name: {namespaced_name}')
        server_name, tool_name = parsed

        with self._lock:
            handle = self._servers.get(server_name)
            if handle is None:
                raise ValueError(f'MCP server not connected: {server_name}')

        timeout = handle.config.get('timeout', MCP_CALL_TIMEOUT)
        logger.info('[MCP:Call] %s.%s(args=%s) timeout=%ds',
                    server_name, tool_name, str(arguments)[:200], timeout)

        t0 = time.time()
        try:
            result = self._run_async(
                self._async_call_tool(handle, tool_name, arguments, timeout)
            )
        except Exception as e:
            elapsed = time.time() - t0
            logger.error('[MCP:Call] %s.%s failed after %.1fs: %s',
                         server_name, tool_name, elapsed, e, exc_info=True)
            raise

        elapsed = time.time() - t0
        logger.info('[MCP:Call] %s.%s returned %d chars in %.1fs',
                    server_name, tool_name, len(result), elapsed)
        return result

    async def _async_call_tool(
        self,
        handle: _MCPServerHandle,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: int,
    ) -> str:
        """Async: call a tool on an MCP server and extract text result."""
        from datetime import timedelta

        result = await handle.session.call_tool(
            tool_name,
            arguments=arguments,
            read_timeout_seconds=timedelta(seconds=timeout),
        )

        # Extract text from the MCP CallToolResult
        if result.isError:
            # MCP reports an error from the tool
            error_text = self._extract_text(result)
            return f'❌ MCP Error: {error_text}'

        text = self._extract_text(result)

        # Truncate if too large
        if len(text) > MCP_MAX_RESULT_CHARS:
            text = text[:MCP_MAX_RESULT_CHARS] + f'\n\n[Truncated: {len(text):,} chars total, showing first {MCP_MAX_RESULT_CHARS:,}]'

        return text

    @staticmethod
    def _extract_text(result) -> str:
        """Extract text content from a CallToolResult.

        MCP results contain a list of content blocks (TextContent,
        ImageContent, etc.).  We extract all text blocks and join them.
        """
        parts = []
        for block in result.content:
            if hasattr(block, 'text'):
                parts.append(block.text)
            elif hasattr(block, 'data'):
                # ImageContent / AudioContent — describe but don't dump binary
                block_type = getattr(block, 'type', 'unknown')
                parts.append(f'[{block_type} content: {len(block.data)} bytes]')
            elif hasattr(block, 'uri'):
                # ResourceLink
                parts.append(f'[Resource: {block.uri}]')
            else:
                parts.append(str(block))
        return '\n'.join(parts)


# ══════════════════════════════════════════════════════════
#  Module-level singleton
# ══════════════════════════════════════════════════════════

_bridge: MCPBridge | None = None
_bridge_lock = threading.Lock()


def get_bridge() -> MCPBridge:
    """Get the global MCPBridge singleton (lazy-initialized)."""
    global _bridge
    if _bridge is not None:
        return _bridge
    with _bridge_lock:
        if _bridge is None:
            _bridge = MCPBridge()
    return _bridge

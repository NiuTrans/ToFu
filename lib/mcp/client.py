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


# ── Launcher install hints ───────────────────────────────

_LAUNCHER_HINTS = {
    'uvx': (
        'Install uv (provides uvx): '
        '`curl -LsSf https://astral.sh/uv/install.sh | sh` '
        '(or `pip install uv`). After install, restart Tofu so the new PATH is picked up.'
    ),
    'npx': (
        'Install Node.js (provides npx): '
        'https://nodejs.org/ (LTS recommended). After install, restart Tofu.'
    ),
    'pipx': (
        'Install pipx: `python3 -m pip install --user pipx && pipx ensurepath`. '
        'After install, restart Tofu.'
    ),
    'node': (
        'Install Node.js: https://nodejs.org/ (LTS recommended).'
    ),
    'python3': (
        'Python 3 is missing from PATH — very unusual. Check your shell PATH.'
    ),
}


def _launcher_install_hint(command: str) -> str:
    """Return an actionable install hint for a missing launcher binary."""
    base = command.rsplit('/', 1)[-1]
    return _LAUNCHER_HINTS.get(base,
        f'Install {command!r} via your package manager, or make sure it is on PATH.'
    )



# ══════════════════════════════════════════════════════════
#  Async core — runs on a dedicated event loop thread
# ══════════════════════════════════════════════════════════

class _MCPServerHandle:
    """Internal handle for a connected MCP server.

    Lifecycle is driven by a dedicated "owner" coroutine (see
    ``MCPBridge._server_owner``).  That coroutine opens the
    ``AsyncExitStack`` holding the stdio/SSE transport + ``ClientSession``
    context managers, signals readiness via ``_ready_future``, then blocks
    on ``_shutdown_event`` until shutdown is requested.  This guarantees
    the context stack is always closed **from the same task that opened
    it**, avoiding the anyio cancel-scope mismatch that would otherwise
    make ``aclose()`` hang for ~130s.
    """

    __slots__ = (
        'name', 'config', 'session', 'tools',
        '_shutdown_event',   # asyncio.Event — set() to request shutdown
        '_ready_future',     # asyncio.Future[list[Tool]] — resolved when init+list_tools done
        '_closed_future',    # asyncio.Future[None] — resolved when owner task exits
        '_owner_task',       # asyncio.Task — the owner coroutine handle
    )

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session = None       # mcp.ClientSession (set after connect)
        self.tools: list = []     # list of mcp.types.Tool
        self._shutdown_event = None
        self._ready_future = None
        self._closed_future = None
        self._owner_task = None


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

    # Shutdown budget for a single server owner task (seconds). The owner
    # coroutine should close its context stack near-instantly once signaled
    # (same task that opened it → no cancel-scope mismatch), so this is
    # really a defense-in-depth cap for pathological cases (e.g. subprocess
    # stuck on a syscall). Intentionally modest to keep the UI responsive.
    _DISCONNECT_TIMEOUT = 5.0

    def connect_server(self, name: str, srv_cfg: dict) -> list:
        """Connect to a single MCP server and discover its tools.

        Args:
            name: Unique server identifier (used as namespace).
            srv_cfg: Server configuration dict.

        Returns:
            List of ``mcp.types.Tool`` objects discovered.
        """
        # Tear down any existing server with the same name BEFORE taking
        # the lock for the new registration. The disconnect itself hits
        # the async loop; holding self._lock across it would freeze every
        # concurrent GET /api/mcp/catalog for the duration.
        had_old = False
        with self._lock:
            had_old = name in self._servers
        if had_old:
            logger.info('[MCP] Reconnecting server %s (was already connected)', name)
            try:
                self._disconnect_one(name)
            except Exception as e:
                # Non-fatal: worst case the OS reaps the stale subprocess
                # when the event loop shuts down. Always log with context.
                logger.warning('[MCP] Error disconnecting old %s: %s', name, e)

        with log_context(f'mcp_connect:{name}', logger=logger):
            handle, tools = self._run_async(self._async_start_owner(name, srv_cfg))

        with self._lock:
            self._servers[name] = handle
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

    async def _async_start_owner(self, name: str, srv_cfg: dict):
        """Async: spawn the owner task for a server and await readiness.

        The owner task holds the ``AsyncExitStack`` open for the lifetime
        of the server (see ``_server_owner``). We return only after the
        session is initialized and the tool list has been fetched.
        """
        loop = asyncio.get_running_loop()
        handle = _MCPServerHandle(name, srv_cfg)
        handle._shutdown_event = asyncio.Event()
        handle._ready_future = loop.create_future()
        handle._closed_future = loop.create_future()

        handle._owner_task = loop.create_task(
            self._server_owner(handle),
            name=f'mcp-owner:{name}',
        )

        # Wait for the owner to finish connect+list_tools (or fail).
        # ``asyncio.shield`` prevents our wait_for timeout from cancelling
        # the owner task itself — if readiness hangs, we still want the
        # owner to complete its own cleanup cycle.
        try:
            tools = await asyncio.wait_for(
                asyncio.shield(handle._ready_future),
                # Generous readiness ceiling: connect handshake + list_tools
                # each have their own MCP_CONNECT_TIMEOUT inside the owner.
                timeout=MCP_CONNECT_TIMEOUT * 2 + 5,
            )
        except asyncio.TimeoutError:
            # Readiness stalled — tell the owner to shut down and re-raise.
            handle._shutdown_event.set()
            raise TimeoutError(
                f'MCP server {name!r}: connection handshake did not complete '
                f'within {MCP_CONNECT_TIMEOUT * 2 + 5}s'
            )
        return handle, tools

    async def _server_owner(self, handle: _MCPServerHandle) -> None:
        """Long-lived owner task: opens the context stack, serves the
        session until shutdown is signaled, then closes the stack from
        within the same task.

        Invariant (the whole point of this refactor): ``aclose()`` on the
        ``AsyncExitStack`` is ALWAYS awaited inside this coroutine, never
        from a different caller. That sidesteps the anyio cancel-scope /
        task-mismatch error that previously caused ``aclose()`` to hang
        for the full ``MCP_CALL_TIMEOUT + 10`` budget (~130s).
        """
        from contextlib import AsyncExitStack

        name = handle.name
        srv_cfg = handle.config

        try:
            async with AsyncExitStack() as stack:
                transport = srv_cfg.get('transport', 'stdio')

                if transport == 'sse':
                    url = srv_cfg.get('url', '')
                    if not url:
                        raise ValueError(
                            f'MCP server {name}: SSE transport requires "url"'
                        )
                    from mcp.client.sse import sse_client
                    read, write = await stack.enter_async_context(sse_client(url))
                else:
                    # stdio transport (default)
                    command = srv_cfg.get('command', '')
                    args = srv_cfg.get('args', [])
                    if not command:
                        raise ValueError(
                            f'MCP server {name}: stdio transport requires "command"'
                        )

                    # Pre-flight: verify the launcher is on PATH. Without this we
                    # get a cryptic FileNotFoundError deep inside mcp.client.stdio.
                    import shutil as _shutil
                    if not _shutil.which(command):
                        hint = _launcher_install_hint(command)
                        raise FileNotFoundError(
                            f'MCP server {name!r}: launcher {command!r} is not on PATH. '
                            f'{hint}'
                        )

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
                await asyncio.wait_for(
                    session.initialize(), timeout=MCP_CONNECT_TIMEOUT
                )

                # Discover tools
                response = await asyncio.wait_for(
                    session.list_tools(), timeout=MCP_CONNECT_TIMEOUT
                )

                handle.session = session
                handle.tools = response.tools

                # Signal readiness BEFORE blocking on the shutdown event.
                if not handle._ready_future.done():
                    handle._ready_future.set_result(response.tools)

                # Serve until shutdown is requested. call_tool runs on the
                # same event loop, just using handle.session directly — no
                # per-call coordination through this task is needed.
                try:
                    await handle._shutdown_event.wait()
                except asyncio.CancelledError:
                    # Someone cancelled the owner task directly (e.g. loop
                    # shutdown). Fall through to AsyncExitStack cleanup.
                    logger.debug('[MCP] Owner %s cancelled — proceeding to cleanup', name)
                # AsyncExitStack.__aexit__ fires here — same task that
                # opened the stack. No cancel-scope mismatch possible.
        except Exception as e:
            # Propagate failure to whoever is awaiting readiness.
            if handle._ready_future and not handle._ready_future.done():
                handle._ready_future.set_exception(e)
            else:
                # Already ready — this was a runtime failure during the
                # shutdown-wait phase. Log with context so we can diagnose.
                logger.warning('[MCP] Owner %s exited with error: %s', name, e)
        finally:
            # Always resolve the closed_future so callers awaiting a
            # clean shutdown are unblocked.
            if handle._closed_future and not handle._closed_future.done():
                handle._closed_future.set_result(None)

    def _disconnect_one(self, name: str) -> None:
        """Sync: request shutdown for a single server and wait (bounded).

        Safe to call from any thread. Runs entirely via ``_run_async``
        indirection so the event loop is touched from the loop thread only.
        """
        with self._lock:
            handle = self._servers.pop(name, None)
            # Remove tool index entries eagerly — the server is gone as
            # far as callers are concerned, even if cleanup is still
            # draining.
            to_remove = [k for k, v in self._tool_index.items()
                         if v['server_name'] == name]
            for k in to_remove:
                del self._tool_index[k]
        if handle is None:
            return

        try:
            self._run_async_with_timeout(
                self._async_signal_shutdown(handle),
                timeout=self._DISCONNECT_TIMEOUT,
            )
        except (asyncio.TimeoutError, TimeoutError) as e:
            # Owner didn't exit in the budget. Force-cancel it on the loop
            # — AsyncExitStack.__aexit__ will still fire (handled via
            # CancelledError inside _server_owner) and clean up the
            # subprocess/pipes.
            logger.warning(
                '[MCP] Disconnect %s did not complete within %.1fs — '
                'force-cancelling owner task (%s)',
                name, self._DISCONNECT_TIMEOUT, e,
            )
            if handle._owner_task is not None and not handle._owner_task.done():
                loop = self._loop
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(handle._owner_task.cancel)

    async def _async_signal_shutdown(self, handle: _MCPServerHandle) -> None:
        """Async: set the shutdown event and await the owner task's exit."""
        if handle._shutdown_event is not None and not handle._shutdown_event.is_set():
            handle._shutdown_event.set()
        if handle._closed_future is not None:
            await handle._closed_future

    def _run_async_with_timeout(self, coro, timeout: float) -> Any:
        """Like ``_run_async`` but with a caller-supplied timeout.

        Use this for disconnect paths where we don't want to pay the
        default ``MCP_CALL_TIMEOUT + 10`` (~130s) — that budget is only
        appropriate for long-running tool calls.
        """
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

    def disconnect_all(self) -> None:
        """Gracefully disconnect all MCP servers."""
        with self._lock:
            names = list(self._servers.keys())
        for name in names:
            try:
                self._disconnect_one(name)
                logger.info('[MCP] Disconnected server: %s', name)
            except Exception as e:
                logger.warning('[MCP] Error disconnecting %s: %s', name, e)
        with self._lock:
            # _disconnect_one already pops; this is belt-and-suspenders
            # in case a caller mutated _servers out from under us.
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

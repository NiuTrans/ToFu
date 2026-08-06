"""lib/mcp/client/_bridge.py — MCPBridge + _MCPServerHandle.

The heavy async core: manages MCP server subprocesses on a dedicated event
loop thread, discovers their tools, translates them to OpenAI
function-calling format, dispatches calls, and runs the keepalive /
credential-health background sweeps.

Facade-routing: the launcher/install/staleness functions that tests
monkeypatch (``_resolve_launcher`` / ``vendored_launch_argv`` /
``_launcher_install_hint`` / ``_check_snapshot_staleness``) are resolved
through ``_pkg()`` at call time so a patch on ``lib.mcp.client`` is honoured.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
from typing import Any

from lib.log import audit_log, get_logger, log_context
from lib.mcp.config import load_mcp_config
from lib.mcp.types import (
    MCP_BREAKER_BASE_BACKOFF,
    MCP_BREAKER_MAX_BACKOFF,
    MCP_CALL_TIMEOUT,
    MCP_CONNECT_TIMEOUT,
    MCP_COLD_INSTALL_TIMEOUT,
    MCP_CRED_PROBE_INTERVAL,
    MCP_DEGRADED_TIMEOUT_STREAK,
    MCP_KEEPALIVE_INTERVAL,
    MCP_MAX_RESULT_CHARS,
    MCP_PING_TIMEOUT,
    MCPToolInfo,
    make_namespaced_name,
    parse_namespaced_name,
)
from lib.mcp.client._state import _pkg
from lib.mcp.client._errors import (
    MCPConnectError,
    _is_call_timeout_error,
    _is_transport_dead_error,
    _read_stderr_tail,
    _unwrap_exception_group,
)
from lib.mcp.client._coerce import _coerce_args_to_schema, _extract_read_only_hint
from lib.mcp.client._vendor import _ensure_writable_caches
from lib.mcp.client._install import _prepend_interpreter_bin_to_path

logger = get_logger(__name__)


def _tool_input_schema(tool) -> dict:
    """The MCP SDK renamed ``Tool.input_schema`` to the spec spelling
    ``inputSchema`` (observed in 1.29.0, installed 2026-08-06 — every server
    connect died on the AttributeError). Accept either; empty → open object."""
    return (getattr(tool, 'input_schema', None) or getattr(tool, 'inputSchema', None)
            or {'type': 'object', 'properties': {}})


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
        'server_name', 'server_version',  # from InitializeResult.serverInfo
        '_shutdown_event',   # asyncio.Event — set() to request shutdown
        '_ready_future',     # asyncio.Future[list[Tool]] — resolved when init+list_tools done
        '_closed_future',    # asyncio.Future[None] — resolved when owner task exits
        '_owner_task',       # asyncio.Task — the owner coroutine handle
        '_stderr_file',      # tempfile.SpooledTemporaryFile capturing child stderr (stdio transport only)
        '_cold_install',     # True when connect_server just evicted this launcher's dep tree
    )

    def __init__(self, name: str, config: dict):
        self.name = name
        self.config = config
        self.session = None       # mcp.ClientSession (set after connect)
        self.tools: list = []     # list of mcp.types.Tool
        self.server_name = ''     # reported by server via InitializeResult.serverInfo.name
        self.server_version = ''  # reported by server via InitializeResult.serverInfo.version
        self._shutdown_event = None
        self._ready_future = None
        self._closed_future = None
        self._owner_task = None
        self._stderr_file = None
        self._cold_install = False


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

        # Per-server reconnect serialization: prevents the reactive
        # (call_tool) and proactive (keepalive) recovery paths from
        # reconnecting the same server concurrently.
        self._reconnect_locks: dict[str, threading.Lock] = {}

        # Dedicated asyncio event loop for MCP sessions
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: threading.Thread | None = None
        self._started = False

        # Background keepalive (proactive health-check + reconnect) loop.
        self._keepalive_task: asyncio.Task | None = None
        self._keepalive_stop: asyncio.Event | None = None

        # Per-server circuit breaker: name → (consecutive_failures, next_retry_ts).
        # Set after a reconnect attempt FAILS; gates the keepalive loop so a
        # permanently-broken server isn't respawned every sweep. Protected by
        # ``self._lock``. Cleared on any successful (re)connect.
        self._breaker: dict[str, tuple[int, float]] = {}

        # Per-server CONSECUTIVE call-timeout streak: name → count. Distinct
        # from the reconnect breaker above — this tracks tool calls that time
        # out on a transport that is otherwise alive. At
        # ``MCP_DEGRADED_TIMEOUT_STREAK`` the server is 'degraded' and the next
        # call fast-fails instead of blocking for the full timeout again. Any
        # successful call resets it to 0. Protected by ``self._lock``.
        self._timeout_streak: dict[str, int] = {}

        # Last-known good config per server. Retained so the keepalive loop
        # can keep retrying a server whose live handle was torn down by a
        # FAILED reconnect (connect_server pops the old handle before it
        # re-registers, so a crash-on-start server would otherwise vanish
        # from ``self._servers`` and never be retried). Protected by ``self._lock``.
        self._configs: dict[str, dict] = {}

        # Per-server CREDENTIAL health: name → {status, checked_at, detail}.
        # A SECOND health axis distinct from transport health — a live
        # subprocess with a valid protocol ping can still hold an EXPIRED
        # session cookie/token, so every real tool call fails. Populated by
        # ``_run_cred_probe`` (once on connect + periodically in keepalive) for
        # servers whose catalog entry declares a ``health_probe``. Surfaced via
        # ``get_cred_health`` → the settings panel. Protected by ``self._lock``.
        #   status ∈ {'ok', 'expired', 'unknown'}
        self._cred_health: dict[str, dict] = {}
        # Monotonic timestamp of the last credential probe per server, so the
        # keepalive sweep re-probes at most once per MCP_CRED_PROBE_INTERVAL.
        self._cred_probe_ts: dict[str, float] = {}

        # Per-server resolved liveness-probe method name (see
        # ``_probe_liveness``): 'send_discover' | 'discover' | 'send_ping' |
        # 'list_tools'. Memoised after a probe answers, so a 2026-07-28 server
        # (whose ``ping`` is -32601) pays the fallback walk once instead of
        # every 45s sweep. Cleared whenever the handle goes away, because the
        # replacement peer may speak a different revision. Protected by
        # ``self._lock``.
        self._probe_method: dict[str, str] = {}

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

    def _run_async(self, coro, timeout: float | None = None) -> Any:
        """Run an async coroutine on the MCP event loop, blocking until done.

        Args:
            coro: The coroutine to drive to completion.
            timeout: Outer (thread-side) wall-clock budget in seconds. The
                inner ``_async_call_tool`` already bounds the request with the
                resolved per-server ``read_timeout_seconds``; this outer cap
                must therefore EXCEED that value (we add 10s of headroom),
                otherwise a server with a longer per-call timeout than the
                global default would be killed here before its own deadline.
                Defaults to ``MCP_CALL_TIMEOUT + 10`` for non-call paths
                (connect / disconnect) that don't carry a per-server budget.
        """
        if timeout is None:
            timeout = (MCP_CALL_TIMEOUT + 10) if MCP_CALL_TIMEOUT else None
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=timeout)

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
            except MCPConnectError as e:
                # Already-formatted root cause + stderr tail. Log the
                # one-line summary at error level; the full chained
                # traceback only at debug.
                logger.error('[MCP] Failed to connect server %s: %s', name, e)
                logger.debug('[MCP] Full traceback for %s:', name,
                             exc_info=(type(e.cause), e.cause, e.cause.__traceback__))
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
        # Detect (and, only if TOFU_MCP_AUTO_VENDOR is set, rebuild) a stale
        # tools/<name> snapshot vs its sibling dev checkout. Always-on path is
        # log-only — never writes git-tracked files by default. No-op for
        # non-vendored commands and on deploys without a sibling.
        try:
            from lib.mcp.transport import stdio_command
            cmd = stdio_command(srv_cfg)
            if cmd and os.sep not in cmd:
                _pkg()._check_snapshot_staleness(cmd)
        except Exception as e:
            logger.debug('[MCP] snapshot staleness check skipped: %s', e)

        # Migrate npx cache slots resolved under a previous supply cutoff.
        # Deliberately BEFORE the readiness timer: npm aborts with
        # ECOMPROMISED against a pre-cutoff lock, so the eviction is required,
        # but the rebuild it forces was measured at 58.6-65.0s against a 65s
        # readiness ceiling -- a coin flip whose losing side looks exactly like
        # a crashed server. Doing it here means the download is not racing the
        # handshake, and `evicted` tells _async_start_owner that a cold
        # dependency fetch is now unavoidable for THIS connect.
        evicted = 0
        try:
            evicted = _pkg().reconcile_for_connect()
        except Exception as e:
            logger.debug('[MCP] npx cache reconcile skipped: %s', e)

        # Tear down any existing server with the same name BEFORE taking
        # the lock for the new registration. The disconnect itself hits
        # the async loop; holding self._lock across it would freeze every
        # concurrent GET /api/v1/mcp/catalog for the duration.
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
            handle, tools = self._run_async(
                self._async_start_owner(name, srv_cfg, cold_install=bool(evicted)))

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
                    input_schema=_tool_input_schema(tool),
                    openai_def=self._tool_to_openai(name, tool),
                    read_only_hint=_extract_read_only_hint(tool),
                )
            self._started = True

        logger.info('[MCP] Server %s connected — %d tools discovered: %s',
                    name, len(tools),
                    ', '.join(t.name for t in tools))
        # A successful (re)connect clears any circuit-breaker backoff and
        # records the working config for future retries.
        with self._lock:
            self._configs[name] = dict(srv_cfg)
            if self._breaker.pop(name, None) is not None:
                logger.info('[MCP] Circuit breaker reset for %s (reconnected)', name)
        self._start_keepalive()
        # Verify stored CREDENTIALS in the background — a live subprocess does
        # not imply a valid session cookie/token. Non-blocking; no-op unless
        # the server's catalog entry declares a health_probe.
        self._probe_cred_health_async(name)
        return tools

    # ── Circuit breaker ──────────────────────────────────

    def _breaker_blocks(self, name: str) -> bool:
        """True if ``name`` is in backoff and its next-retry time hasn't arrived.

        Read-only check used by the keepalive sweep to skip servers that are
        currently being backed off after repeated reconnect failures.
        """
        with self._lock:
            entry = self._breaker.get(name)
        if entry is None:
            return False
        _, next_retry_ts = entry
        return time.time() < next_retry_ts

    def get_breaker_state(self, name: str) -> dict[str, Any] | None:
        """Return the circuit-breaker status for ``name`` for UI surfacing.

        Returns ``None`` when the server has no recorded reconnect failures
        (healthy / never failed). Otherwise a dict::

            {
              'failures': int,        # consecutive failed reconnects
              'retry_in': float,      # seconds until next retry (>=0)
              'next_retry_ts': float, # absolute epoch seconds
            }

        ``retry_in`` is clamped to 0 when the backoff window has already
        elapsed (a retry is due on the next keepalive sweep).
        """
        with self._lock:
            entry = self._breaker.get(name)
        if entry is None:
            return None
        failures, next_retry_ts = entry
        return {
            'failures': failures,
            'retry_in': max(0.0, next_retry_ts - time.time()),
            'next_retry_ts': next_retry_ts,
        }

    # ── Credential health (transport-alive but stored secret expired) ──

    def get_cred_health(self, name: str) -> dict[str, Any] | None:
        """Return the last credential-probe result for ``name`` for UI surfacing.

        Returns ``None`` when the server has never been probed (no
        ``health_probe`` declared, or not yet run). Otherwise a dict::

            {
              'status': 'ok' | 'expired' | 'unknown',
              'checked_at': float,   # epoch seconds of the last probe
              'detail': str,         # short reason (empty for 'ok')
            }

        Only ``status == 'expired'`` is actionable — the UI shows a
        "credentials expired" badge. ``'unknown'`` (probe raised / could not
        classify) is deliberately NOT surfaced as a failure so a transient
        network blip never cries wolf about a still-valid cookie.
        """
        with self._lock:
            entry = self._cred_health.get(name)
        return dict(entry) if entry is not None else None

    def _cred_probe_spec(self, name: str) -> dict | None:
        """Resolve + validate the ``health_probe`` spec for ``name``.

        The standard credential-health contract (see lib/mcp/health_probe.py)
        can be declared TWO ways, checked in priority order:

          1. The server's LIVE config (``handle.config`` / ``self._configs``) —
             so a user's custom ``mcp_servers.json`` entry can opt in with a
             ``"health_probe": {...}`` key, no code change needed.
          2. The curated catalog entry (late import to avoid a registry↔client
             cycle) — how in-tree servers like Overleaf ship a default probe.

        Returns a NORMALIZED spec (``{tool, args, fail_patterns}`` with the
        default auth patterns merged in) or ``None`` when no valid probe is
        declared. A malformed probe logs a warning via ``validate_health_probe``
        and resolves to ``None`` (never crashes the sweep).
        """
        from lib.mcp.health_probe import validate_health_probe

        # 1. Live config (custom servers + any config override).
        with self._lock:
            handle = self._servers.get(name)
            cfg = dict(handle.config) if handle is not None else self._configs.get(name)
        raw = cfg.get('health_probe') if isinstance(cfg, dict) else None

        # 2. Catalog default.
        if raw is None:
            try:
                from lib.mcp.registry import get_catalog_entry
                entry = get_catalog_entry(name)
                raw = entry.get('health_probe') if entry else None
            except Exception as e:
                logger.debug('[MCP] cred-probe catalog lookup for %s failed: %s',
                             name, e)
                raw = None

        return validate_health_probe(raw, server=name)

    def _run_cred_probe(self, name: str) -> dict | None:
        """Run the credential health probe for ``name`` and record the result.

        SYNC — safe to call from a worker thread (it drives ``call_tool``,
        which owns its own event-loop indirection). Best-effort: any failure
        classifies as ``'unknown'`` and never raises into the caller (the
        keepalive sweep must not die on a probe error).

        Returns the recorded health dict, or None when there is no probe to run
        (no spec / server not connected).
        """
        spec = self._cred_probe_spec(name)
        if spec is None:
            return None
        with self._lock:
            connected = name in self._servers
        if not connected:
            return None

        from lib.mcp.health_probe import classify_probe_result

        tool = spec['tool']
        args = spec.get('args', {}) or {}
        ns_name = make_namespaced_name(name, tool)

        status = 'unknown'
        detail = ''
        try:
            result = self.call_tool(ns_name, dict(args))
            # PURE classifier owns the ok/expired verdict; a RAISED call (below)
            # is the only path to 'unknown' — a transport blip must never be
            # mislabelled as an expired credential.
            status, detail = classify_probe_result(result, spec)
            if status == 'expired':
                logger.warning('[MCP] Credential probe for %s → EXPIRED (%s)',
                               name, detail)
            else:
                logger.debug('[MCP] Credential probe for %s → ok', name)
        except Exception as e:
            status = 'unknown'
            detail = str(e)[:200]
            logger.debug('[MCP] Credential probe for %s raised (%s) — '
                         'reported as unknown', name, _unwrap_exception_group(e))

        now = time.time()
        record = {'status': status, 'checked_at': now, 'detail': detail}
        with self._lock:
            self._cred_health[name] = record
            self._cred_probe_ts[name] = now
        if status == 'expired':
            audit_log('mcp_credential_expired', server=name, tool=tool)
        return record

    def _cred_probe_due(self, name: str) -> bool:
        """True if ``name`` has a health_probe and its re-probe interval elapsed.

        Gates the keepalive sweep so a credential is re-checked at most once per
        MCP_CRED_PROBE_INTERVAL. Returns False when the periodic probe is
        disabled (interval <= 0) or the server declares no probe.
        """
        if MCP_CRED_PROBE_INTERVAL <= 0:
            return False
        if self._cred_probe_spec(name) is None:
            return False
        with self._lock:
            last = self._cred_probe_ts.get(name, 0.0)
        return (time.time() - last) >= MCP_CRED_PROBE_INTERVAL

    def _probe_cred_health_async(self, name: str) -> None:
        """Fire a one-shot credential probe on a daemon thread (non-blocking).

        Used at connect time so a fresh connection surfaces an expired cookie
        promptly WITHOUT blocking the connect/handshake path. No-op for servers
        with no declared ``health_probe``.
        """
        if MCP_CRED_PROBE_INTERVAL < 0:
            return
        if self._cred_probe_spec(name) is None:
            return

        def _worker():
            try:
                self._run_cred_probe(name)
            except Exception as e:  # defence in depth — _run_cred_probe swallows
                logger.debug('[MCP] async cred probe for %s failed: %s', name, e)

        threading.Thread(target=_worker, name=f'mcp-credprobe-{name}',
                         daemon=True).start()

    def _breaker_record_failure(self, name: str) -> float:
        """Record a failed reconnect and return the backoff delay applied.

        Bumps the consecutive-failure count and schedules the next allowed
        retry at ``min(BASE * 2**(failures-1), MAX)`` seconds from now.
        """
        with self._lock:
            failures = self._breaker.get(name, (0, 0.0))[0] + 1
            delay = min(
                MCP_BREAKER_BASE_BACKOFF * (2 ** (failures - 1)),
                MCP_BREAKER_MAX_BACKOFF,
            )
            self._breaker[name] = (failures, time.time() + delay)
        return delay

    def _reconnect_server(self, name: str) -> _MCPServerHandle:
        """Tear down and re-establish a single server using its stored config.

        Used by both recovery paths (reactive call_tool retry + proactive
        keepalive). Serialized per-server via ``_reconnect_locks`` so two
        callers racing on the same dropped server don't spawn duplicate
        subprocesses. Returns the fresh handle.

        Raises if the config is unknown or the reconnect itself fails — the
        caller decides how to surface that.
        """
        with self._lock:
            old = self._servers.get(name)
            # Prefer the live handle's config; fall back to the last-known-good
            # config so a server torn down by a previous FAILED reconnect can
            # still be retried.
            srv_cfg = dict(old.config) if old is not None else self._configs.get(name)
            srv_cfg = dict(srv_cfg) if srv_cfg is not None else None
            rlock = self._reconnect_locks.setdefault(name, threading.Lock())
        if srv_cfg is None:
            raise ValueError(f'cannot reconnect unknown MCP server: {name}')

        with rlock:
            # Re-check under the per-server lock: a racing caller may have
            # already reconnected while we waited. If the live handle has a
            # session and differs from the one we saw, reuse it.
            with self._lock:
                cur = self._servers.get(name)
            if cur is not None and cur is not old and cur.session is not None:
                logger.info('[MCP] %s already reconnected by another caller', name)
                return cur
            audit_log('mcp_reconnect', server=name)
            logger.info('[MCP] Reconnecting server %s', name)
            try:
                self.connect_server(name, srv_cfg)
            except Exception:
                # Record the failure so the circuit breaker backs off the
                # next keepalive sweep (connect_server clears the breaker on
                # success, so this only sticks for genuinely failing servers).
                delay = self._breaker_record_failure(name)
                with self._lock:
                    failures = self._breaker.get(name, (0, 0.0))[0]
                logger.warning(
                    '[MCP] Reconnect of %s failed (consecutive=%d) — '
                    'backing off %.0fs before next attempt',
                    name, failures, delay,
                )
                raise
            with self._lock:
                return self._servers[name]

    async def _async_start_owner(self, name: str, srv_cfg: dict,
                                 cold_install: bool = False):
        """Async: spawn the owner task for a server and await readiness.

        The owner task holds the ``AsyncExitStack`` open for the lifetime
        of the server (see ``_server_owner``). We return only after the
        session is initialized and the tool list has been fetched.

        ``cold_install`` widens the readiness budget for the ONE case we can
        positively identify: the caller just evicted this launcher's dependency
        tree (stale supply cutoff), so a full download must finish before the
        server can even speak. Measured: an npx rebuild takes 27-65s while a
        warm start is 4-8s, so the ordinary ceiling turns that migration into a
        coin flip. The default ceiling is deliberately NOT raised -- a server
        that never comes up must still fail fast (lib/mcp/types.py:18-25:
        a handshake that never completes is a crash, not a wait).
        """
        loop = asyncio.get_running_loop()
        handle = _MCPServerHandle(name, srv_cfg)
        handle._cold_install = cold_install
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
        # Readiness ceiling: connect handshake + list_tools each have their
        # own MCP_CONNECT_TIMEOUT inside the owner. When a dependency tree was
        # just evicted, the download is serialized ahead of the handshake, so
        # the budget is widened for that identified case only.
        ready_budget = (MCP_COLD_INSTALL_TIMEOUT if cold_install
                        else MCP_CONNECT_TIMEOUT * 2 + 5)
        if cold_install:
            logger.info('[MCP] %s: dependency tree was just migrated — '
                        'allowing %ds for the cold download instead of %ds',
                        name, ready_budget, MCP_CONNECT_TIMEOUT * 2 + 5)
        try:
            tools = await asyncio.wait_for(
                asyncio.shield(handle._ready_future),
                timeout=ready_budget,
            )
        except asyncio.TimeoutError:
            # Readiness stalled — tell the owner to shut down and re-raise
            # as a MCPConnectError so the route can surface a user-facing
            # message including any captured stderr.
            handle._shutdown_event.set()
            stderr_tail = _read_stderr_tail(handle._stderr_file)
            raise MCPConnectError(
                name,
                TimeoutError(
                    f'connection handshake did not complete within '
                    f'{ready_budget}s'
                ),
                stderr_tail,
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
                from lib.mcp.transport import (
                    SSE, STREAMABLE_HTTP, is_stdio, normalize_transport,
                    resolve_headers, resolve_url,
                )
                transport = normalize_transport(srv_cfg)

                if not is_stdio(srv_cfg):
                    if not srv_cfg.get('url'):
                        raise ValueError(
                            f'MCP server {name}: {transport} transport '
                            f'requires "url"'
                        )
                    # Both credential carriers are templated against the
                    # server's env block, so the secret has ONE home: a header
                    # (Bearer) or a query param (Amap ?key=). A missing
                    # credential raises here naming the key instead of
                    # becoming an opaque upstream 401. ``url`` now carries a
                    # live secret — never log it (use scrub_text).
                    url = resolve_url(srv_cfg, server_name=name)
                    hdrs = resolve_headers(srv_cfg, server_name=name)
                    if transport == STREAMABLE_HTTP:
                        # v2 signature: headers no longer go to the transport
                        # directly — they ride on an httpx2.AsyncClient built
                        # by the SDK's own factory. We provide the client (so
                        # the transport will NOT close it) and enter it into
                        # OUR stack so its lifecycle matches the session's.
                        from mcp.client.streamable_http import (
                            create_mcp_http_client,
                            streamable_http_client,
                        )
                        http_client = create_mcp_http_client(headers=hdrs or None)
                        await stack.enter_async_context(http_client)
                        read, write = await stack.enter_async_context(
                            streamable_http_client(url, http_client=http_client)
                        )
                    elif transport == SSE:
                        from mcp.client.sse import sse_client
                        read, write = await stack.enter_async_context(
                            sse_client(url, headers=hdrs or None)
                        )
                    else:
                        raise ValueError(
                            f'MCP server {name}: unknown remote transport '
                            f'{transport!r}'
                        )
                else:
                    # stdio transport (default)
                    command = srv_cfg.get('command', '')
                    args = srv_cfg.get('args', [])
                    if not command:
                        raise ValueError(
                            f'MCP server {name}: stdio transport requires "command"'
                        )

                    # Pre-flight: verify the launcher is resolvable. Without
                    # this we get a cryptic FileNotFoundError deep inside
                    # mcp.client.stdio.
                    #
                    # Vendored internal servers are translated FIRST: a bare
                    # name like ``hope-mcp`` becomes
                    # ``uv run --no-project --with-editable <src> hope-mcp``,
                    # so the server resolves its own dependency tree into its
                    # OWN environment and its ``mcp`` never couples to Tofu's
                    # interpreter. This must happen before the PATH checks —
                    # a stale pip-installed console script in the shared env
                    # is precisely what we must NOT launch.
                    launch_argv = _pkg().vendored_launch_argv(command)
                    if launch_argv is not None:
                        logger.info(
                            '[MCP] %s: launching vendored server isolated: %s',
                            name, ' '.join(launch_argv))
                        command = launch_argv[0]
                        args = launch_argv[1:] + list(args)

                    import shutil as _shutil
                    if not _shutil.which(command):
                        # Not on PATH: try resolving a console script that
                        # lives next to the running interpreter (the common
                        # "installed but not on the subprocess PATH" case)
                        # before giving up.
                        resolved = _pkg()._resolve_launcher(command)
                        if resolved:
                            command = resolved
                        else:
                            hint = _pkg()._launcher_install_hint(command)
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
                    # Redirect launcher caches (uv/uvx, npm/npx, pip) to a
                    # writable project-local dir when $HOME/.cache is read-only
                    # — otherwise uvx dies before the MCP handshake.
                    _ensure_writable_caches(env)
                    # Make the running interpreter's bin/ dir visible to the
                    # child. This is what lets a pip-installed MCP server find
                    # its sibling console tools (e.g. hope-mcp shelling out to
                    # `hope`) AND mirrors the _resolve_launcher fallback so the
                    # subprocess sees the same launcher we resolved. Prepended
                    # so the env Tofu runs in wins over a stale system copy.
                    _prepend_interpreter_bin_to_path(env)
                    extra_env = srv_cfg.get('env', {})
                    if extra_env:
                        env.update(extra_env)

                    params = StdioServerParameters(
                        command=command,
                        args=args,
                        env=env,
                    )

                    # Capture the child's stderr to a real on-disk tempfile so
                    # that if the launcher dies during the MCP handshake, we
                    # can surface the actual error (Python traceback, "module
                    # not found", auth failure, etc.) to the user instead of
                    # a useless "Connection closed". A real file (with
                    # ``fileno()``) is required: mcp.client.stdio passes
                    # ``errlog`` straight to ``anyio.open_process(stderr=...)``,
                    # which only accepts an fd-backed file or integer fd.
                    # The file is unlinked from disk on close (mode w+b,
                    # delete=True) so it never accumulates.
                    stderr_buf = tempfile.TemporaryFile(mode='w+b')
                    handle._stderr_file = stderr_buf
                    read, write = await stack.enter_async_context(
                        stdio_client(params, errlog=stderr_buf)
                    )

                # Create and initialize session
                from mcp import ClientSession

                session = await stack.enter_async_context(
                    ClientSession(read, write)
                )
                # Handshake budget. When this launcher's dependency tree was
                # just evicted, npx must finish DOWNLOADING before the spawned
                # process answers a single JSON-RPC byte — so the wait lands
                # here, on initialize(), not on process spawn. Measured: the
                # outer readiness budget alone was NOT sufficient (a trial
                # still failed at 67.5s with 300s granted outside), because
                # this inner 30s timer fired first.
                handshake_budget = (MCP_COLD_INSTALL_TIMEOUT
                                    if getattr(handle, '_cold_install', False)
                                    else MCP_CONNECT_TIMEOUT)
                init_result = await asyncio.wait_for(
                    session.initialize(), timeout=handshake_budget
                )
                # Harvest serverInfo.name / serverInfo.version so the UI
                # can surface the upstream MCP server's own version (not
                # Tofu's or the launcher's). This comes from the MCP
                # handshake — see mcp.types.Implementation.
                try:
                    srv_info = getattr(init_result, 'server_info', None)
                    if srv_info is not None:
                        handle.server_name = str(getattr(srv_info, 'name', '') or '')
                        handle.server_version = str(getattr(srv_info, 'version', '') or '')
                        if handle.server_version:
                            logger.info(
                                '[MCP] Server %s reports version %s (impl=%s)',
                                name, handle.server_version, handle.server_name or '?',
                            )
                except Exception as e:
                    logger.debug('[MCP] Could not parse serverInfo for %s: %s', name, e)

                # Discover tools
                response = await asyncio.wait_for(
                    session.list_tools(), timeout=handshake_budget
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
        except BaseException as e:
            # anyio / mcp wrap the real failure in nested ExceptionGroups —
            # unwrap before forwarding so callers see the actual cause
            # (e.g. ``McpError: Connection closed``, ``FileNotFoundError``)
            # instead of the unhelpful "unhandled errors in a TaskGroup".
            root = _unwrap_exception_group(e)
            stderr_tail = _read_stderr_tail(handle._stderr_file)
            if stderr_tail:
                logger.warning(
                    '[MCP] Server %s stderr tail (%d chars):\n%s',
                    name, len(stderr_tail), stderr_tail,
                )
            wrapped = MCPConnectError(name, root, stderr_tail)
            # Propagate failure to whoever is awaiting readiness.
            if handle._ready_future and not handle._ready_future.done():
                handle._ready_future.set_exception(wrapped)
            else:
                # Already ready — this was a runtime failure during the
                # shutdown-wait phase. Log with context so we can diagnose.
                logger.warning('[MCP] Owner %s exited with error: %s', name, wrapped)
            # Re-raise CancelledError so the event loop can finish its job.
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            # Always resolve the closed_future so callers awaiting a
            # clean shutdown are unblocked.
            if handle._closed_future and not handle._closed_future.done():
                handle._closed_future.set_result(None)
            # Release the stderr capture buffer.
            if handle._stderr_file is not None:
                try:
                    handle._stderr_file.close()
                except OSError as e:
                    logger.debug('[MCP] stderr_file close failed: %s', e)
                handle._stderr_file = None

    def _disconnect_one(self, name: str, forget: bool = False) -> None:
        """Sync: request shutdown for a single server and wait (bounded).

        Safe to call from any thread. Runs entirely via ``_run_async``
        indirection so the event loop is touched from the loop thread only.

        Args:
            name: server id to disconnect.
            forget: when True (explicit user disconnect / removal), also drop
                the circuit-breaker state and stored config so the keepalive
                loop stops retrying it. The reconnect teardown path leaves
                this False so a transient reconnect doesn't erase recovery
                state.
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
            if forget:
                self._breaker.pop(name, None)
                self._configs.pop(name, None)
            # Credential-health is transient live state — always drop it when
            # the handle goes away (a reconnect re-probes fresh on connect).
            self._cred_health.pop(name, None)
            self._cred_probe_ts.pop(name, None)
            # The reconnected peer may speak a different protocol revision, so
            # the resolved probe must be re-derived rather than inherited.
            self._probe_method.pop(name, None)
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
        # Stop the keepalive loop first so it can't race with teardown by
        # reconnecting a server we're about to drop. Set the stop event AND
        # cancel the task: the task may be parked in ``wait_for(stop.wait())``
        # or mid-sweep, and we're about to stop the loop out from under it.
        loop = self._loop
        task = self._keepalive_task
        if loop is not None and loop.is_running():
            def _stop_keepalive():
                if self._keepalive_stop is not None:
                    self._keepalive_stop.set()
                if task is not None and not task.done():
                    task.cancel()
            loop.call_soon_threadsafe(_stop_keepalive)
        self._keepalive_task = None

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
            self._breaker.clear()
            self._configs.clear()
            self._cred_health.clear()
            self._cred_probe_ts.clear()
            self._probe_method.clear()
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
        schema = dict(_tool_input_schema(tool))
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

    # ── Per-tool enable/disable (pt_53065dbe86bb4286) ─────────
    # The user disables individual tools of a server in Settings → MCP; the
    # list lives in the server config row (``disabled_tools``) so it survives
    # restarts and rides the existing config plumbing (migrations preserve
    # unknown keys). A disabled tool is BOTH hidden from the model's tool
    # list (schema diet) and refused at call time (stale-history / in-flight
    # protection).

    def _disabled_tools_for(self, server_name: str) -> frozenset:
        """Tool names the user disabled on this server.

        Read from the last-known-good config row, which the PUT endpoint
        keeps current via :meth:`set_disabled_tools`. getattr-tolerant:
        minimal fake bridges in tests skip ``__init__``.
        """
        configs = getattr(self, '_configs', None) or {}
        row = configs.get(server_name) or {}
        return frozenset(t for t in (row.get('disabled_tools') or [])
                         if isinstance(t, str))

    def set_disabled_tools(self, server_name: str, tool_names) -> None:
        """Hot-update the live per-server tool filter.

        Persistence is the caller's job (routes/api_v1/mcp.py writes the
        config row first); this makes the running bridge honour the change
        without a reconnect.
        """
        names = sorted({t for t in (tool_names or []) if isinstance(t, str)})
        with self._lock:
            row = self._configs.setdefault(server_name, {})
            row['disabled_tools'] = names
            handle = self._servers.get(server_name)
            if handle is not None and isinstance(getattr(handle, 'config', None), dict):
                handle.config['disabled_tools'] = names

    # ── Tool discovery (for LLM) ──────────────────────────

    def get_openai_tool_defs(self) -> list[dict[str, Any]]:
        """Get all MCP tools as OpenAI function-calling definitions.

        Returns:
            List of OpenAI tool dicts ready to append to the tool_list.

        Ordering is deterministic (sorted by namespaced name) rather than
        dict-insertion order.  Insertion order changes whenever a server
        reconnects or re-discovers its tools mid-conversation, which would
        reorder the tools array and break the prompt cache prefix even though
        the tool *content* is unchanged.  A stable sort keeps the bytes
        identical across rounds.
        """
        with self._lock:
            return [info['openai_def']
                    for ns, info in sorted(self._tool_index.items())
                    if info['tool_name']
                    not in self._disabled_tools_for(info['server_name'])]

    def get_tool_safety(self) -> dict[str, bool]:
        """Map every discovered MCP tool's namespaced name → read-only flag.

        The flag is the tool's MCP ``annotations.readOnlyHint`` (default
        ``False`` when the server omits it). Consumers use this to partition
        MCP tools for concurrency / write-approval: a tool that is NOT
        read-only is treated as a write tool (serial + approval-eligible).
        """
        with self._lock:
            return {ns: bool(info.get('read_only_hint'))
                    for ns, info in self._tool_index.items()
                    if info['tool_name']
                    not in self._disabled_tools_for(info['server_name'])}

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
                    'server_version': handle.server_version,
                    'server_impl_name': handle.server_name,
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
            if tool_name in self._disabled_tools_for(server_name):
                raise ValueError(
                    f'MCP tool disabled by user: {namespaced_name} '
                    f'(re-enable it in Settings → MCP)')

        # Coerce LLM-provided strings to the schema's declared types.
        # LLMs that don't strictly honor the JSON schema (esp. weaker models)
        # frequently emit `"step_version": "1"` for an integer field, which
        # the MCP server's jsonschema validator then rejects with
        # `'1' is not of type 'integer'`. Best-effort coerce so the call
        # actually reaches the server.
        info = self._tool_index.get(namespaced_name)
        if info is not None:
            arguments = _coerce_args_to_schema(arguments, info['input_schema'])
            # Use the server's actual registered tool name. ``parse_namespaced_name``
            # only sees the post-dedupe view, but the MCP server itself was
            # registered with the original (possibly stuttering) name.
            tool_name = info['tool_name']

        timeout = handle.config.get('timeout', MCP_CALL_TIMEOUT)

        # ── Call-level health gate ──
        # If this server has already timed out MCP_DEGRADED_TIMEOUT_STREAK
        # times in a row, fast-fail with an actionable error instead of
        # blocking the model for another full ``timeout`` seconds (and then
        # likely auto-retrying). A single later success resets the streak.
        if MCP_DEGRADED_TIMEOUT_STREAK > 0:
            with self._lock:
                streak = self._timeout_streak.get(server_name, 0)
            if streak >= MCP_DEGRADED_TIMEOUT_STREAK:
                logger.warning(
                    '[MCP:Call] %s.%s SKIPPED — server degraded after %d '
                    'consecutive call timeouts (timeout=%s). Not retried; '
                    'reconnect or raise the per-server timeout to recover.',
                    server_name, tool_name, streak,
                    f'{timeout}s' if timeout else 'none',
                )
                audit_log('mcp_server_degraded', server=server_name,
                          tool=tool_name, consecutive_timeouts=streak,
                          timeout=timeout)
                return (
                    f'MCP Error: server {server_name!r} is degraded — '
                    f'{streak} consecutive call timeouts (each waited '
                    f'{timeout}s). The call was not attempted. This usually '
                    f'means the tool runs longer than the server\'s per-call '
                    f'timeout; do not retry blindly.'
                )

        logger.info('[MCP:Call] %s.%s(args=%s) timeout=%s',
                    server_name, tool_name, str(arguments)[:200],
                    f'{timeout}s' if timeout else 'none')

        t0 = time.time()
        try:
            result = self._run_async(
                self._async_call_tool(handle, tool_name, arguments, timeout),
                timeout=(timeout + 10) if timeout else None,
            )
        except Exception as e:
            elapsed = time.time() - t0
            # A call timeout (transport read-timeout OR the outer thread-future
            # budget) bumps the per-server streak and leaves an audit trail so
            # the mismatch between a long-poll tool and the transport cap is
            # greppable. NOT treated as a transport-dead error (reconnecting
            # wouldn't help a tool that simply runs longer than its budget).
            if _is_call_timeout_error(e):
                with self._lock:
                    new_streak = self._timeout_streak.get(server_name, 0) + 1
                    self._timeout_streak[server_name] = new_streak
                logger.warning(
                    '[MCP:Call] %s.%s TIMED OUT after %.1fs (budget=%ds, '
                    'consecutive=%d). If this tool legitimately runs longer '
                    'than %ds, raise its per-server "timeout" in '
                    'mcp_servers.json.',
                    server_name, tool_name, elapsed, timeout, new_streak, timeout,
                )
                audit_log('mcp_call_timeout', server=server_name,
                          tool=tool_name, timeout=timeout,
                          elapsed=round(elapsed, 1), consecutive=new_streak)
                raise
            # If the call failed because the transport is dead (subprocess
            # crashed / idle-dropped), transparently reconnect once and
            # retry — the user should never have to manually reconnect.
            if _is_transport_dead_error(e):
                logger.warning(
                    '[MCP:Call] %s.%s hit dead transport after %.1fs (%s) — '
                    'reconnecting and retrying once',
                    server_name, tool_name, elapsed, e,
                )
                try:
                    new_handle = self._reconnect_server(server_name)
                except Exception as re:
                    logger.error('[MCP:Call] reconnect of %s failed: %s',
                                 server_name, re, exc_info=True)
                    raise e from re
                try:
                    result = self._run_async(
                        self._async_call_tool(new_handle, tool_name, arguments, timeout),
                        timeout=(timeout + 10) if timeout else None,
                    )
                    elapsed = time.time() - t0
                    self._reset_timeout_streak(server_name)
                    logger.info(
                        '[MCP:Call] %s.%s succeeded after reconnect+retry '
                        '(%d chars in %.1fs)',
                        server_name, tool_name, len(result), elapsed,
                    )
                    return result
                except Exception as e2:
                    logger.error('[MCP:Call] %s.%s still failing after reconnect: %s',
                                 server_name, tool_name, e2, exc_info=True)
                    raise
            logger.error('[MCP:Call] %s.%s failed after %.1fs: %s',
                         server_name, tool_name, elapsed, e, exc_info=True)
            raise

        elapsed = time.time() - t0
        # Any successful call clears the degraded streak.
        self._reset_timeout_streak(server_name)
        logger.info('[MCP:Call] %s.%s returned %d chars in %.1fs',
                    server_name, tool_name, len(result), elapsed)
        return result

    def _reset_timeout_streak(self, server_name: str) -> None:
        """Clear a server's consecutive call-timeout streak (called on any
        successful call). Logs once when recovering from a degraded state."""
        with self._lock:
            prev = self._timeout_streak.pop(server_name, 0)
        if prev >= MCP_DEGRADED_TIMEOUT_STREAK > 0:
            logger.info('[MCP:Call] %s recovered from degraded state '
                        '(streak was %d)', server_name, prev)

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
            # None = no read timeout (the default). A per-server "timeout" in
            # mcp_servers.json still bounds that server's calls, and that is
            # what keeps the degraded-streak breaker meaningful.
            read_timeout_seconds=timedelta(seconds=timeout) if timeout else None,
        )

        # Extract text from the MCP CallToolResult
        if result.is_error:
            # MCP reports an error from the tool
            error_text = self._extract_text(result)
            return f'MCP Error: {error_text}'

        text = self._extract_text(result)

        # Truncate if too large
        if len(text) > MCP_MAX_RESULT_CHARS:
            text = text[:MCP_MAX_RESULT_CHARS] + f'\n\n[Truncated: {len(text):,} chars total, showing first {MCP_MAX_RESULT_CHARS:,}]'

        return text

    # ── Keepalive: proactive health-check + auto-reconnect ──

    def _start_keepalive(self) -> None:
        """Launch the background keepalive loop on the MCP event loop.

        Idempotent and a no-op when disabled
        (``TOFU_MCP_KEEPALIVE_INTERVAL=0``). Called after the first
        successful connect so idle deployments don't spin a loop for
        nothing.
        """
        if MCP_KEEPALIVE_INTERVAL <= 0:
            return
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        if self._keepalive_task is not None and not self._keepalive_task.done():
            return

        def _spawn():
            self._keepalive_stop = asyncio.Event()
            self._keepalive_task = loop.create_task(
                self._keepalive_loop(), name='mcp-keepalive')

        loop.call_soon_threadsafe(_spawn)
        logger.info('[MCP] Keepalive loop armed (interval=%ds, ping_timeout=%ds)',
                    MCP_KEEPALIVE_INTERVAL, MCP_PING_TIMEOUT)

    @staticmethod
    def _probe_callable(session, meth: str) -> bool:
        """True when ``session.meth`` exists and is callable with NO arguments.

        Arity matters because the probe list spans SDK majors and the same
        name can carry different signatures: measured on mcp 2.0.0,
        ``discover()`` takes nothing but ``send_discover(version)`` REQUIRES a
        protocol version. Calling the latter bare raises ``TypeError`` — and a
        TypeError is OUR bug, not evidence about the peer, so letting it reach
        the liveness verdict would reconnect a healthy server. That is exactly
        the defect class this module exists to remove, so the arity is checked
        rather than assumed.

        Unintrospectable callables (C extensions, exotic mocks) are accepted:
        being unable to read a signature is not evidence the call is wrong.
        """
        import inspect
        fn = getattr(session, meth, None)
        if not callable(fn):
            return False
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError) as e:
            logger.debug('[MCP] cannot introspect %s.%s (%s) — accepting callable',
                         type(session).__name__, meth, e)
            return True
        for p in sig.parameters.values():
            if p.name == 'self':
                continue
            if p.default is inspect.Parameter.empty and p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                return False
        return True

    async def _probe_liveness(self, name: str, session) -> str:
        """Ask the peer one cheap question and report whether it ANSWERED.

        Returns ``'alive'`` or ``'dead'``. Never raises.

        WHY THIS IS NOT ``send_ping``
        -----------------------------
        Protocol revision 2026-07-28 REMOVED ``ping`` from the schema outright
        (measured: 0 occurrences in the published schema.ts — unlike
        ``logging/setLevel``, which survives with an ``@deprecated`` tag and a
        12-month window). Both SDK majors still expose ``send_ping`` for
        compatibility with older servers, and the v2 low-level ``Server`` still
        registers a default ``on_ping`` — but neither fact binds a *server*.
        A conforming 2026-07-28 server answers ``-32601 Method not found``,
        and the old probe read that as "transport dead" and reconnected. On a
        45s sweep that is a reconnect every 45s, forever, against a server that
        is working perfectly.

        THE VERDICT IS "DID THE PEER ANSWER", NOT "DID THE CALL SUCCEED"
        ---------------------------------------------------------------
        Any JSON-RPC error response proves the round trip completed, so it
        counts as alive (see ``_is_peer_answered_error``). Only a timeout or a
        transport-layer failure means dead. This is what makes the check
        correct across every protocol revision rather than for one of them.

        PROBE SELECTION
        ---------------
        Ordered by protocol standing, resolved against what the live session
        actually offers, and memoised per server once one works:

          1. ``server/discover`` — the 2026-07-28 RPC that servers **MUST**
             implement; semantically exactly "are you there + what can you do".
             Only present on SDK v2 sessions.
          2. ``send_ping`` — correct for pre-2026-07-28 servers, which is what
             Tofu's own ``mcp<2`` pin still talks to today.
          3. ``list_tools`` — present in every revision including 2026-07-28,
             so it is the floor that always exists.

        A ``-32601`` from a probe means "this peer does not implement THIS
        method" (not "this peer is dead"), so we record that and try the next
        candidate; the peer already proved it is alive by answering at all.
        """
        from lib.mcp.client._errors import (
            _is_method_not_found, _is_peer_answered_error,
        )

        candidates: list[str] = []
        preferred = self._probe_method.get(name)
        if preferred:
            candidates.append(preferred)
        for meth in ('discover', 'send_discover', 'send_ping', 'list_tools'):
            if meth not in candidates and self._probe_callable(session, meth):
                candidates.append(meth)

        if not candidates:
            # Nothing to ask with — a session object this foreign is not
            # something we should declare dead on; leave it to the reactive
            # call path, which sees real traffic.
            logger.debug('[MCP] %s: no usable liveness probe on session; '
                         'skipping health sweep', name)
            return 'alive'

        last_exc: BaseException | None = None
        for meth in candidates:
            if not self._probe_callable(session, meth):
                continue
            fn = getattr(session, meth)
            try:
                await asyncio.wait_for(fn(), timeout=MCP_PING_TIMEOUT)
                if self._probe_method.get(name) != meth:
                    with self._lock:
                        self._probe_method[name] = meth
                    logger.info('[MCP] %s: health probe resolved to %s()',
                                name, meth)
                return 'alive'
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_exc = e
                if _is_method_not_found(e):
                    # The peer ANSWERED — it just doesn't implement this RPC.
                    # That is a liveness proof; try the next candidate so the
                    # memo lands on one this peer actually supports.
                    logger.debug('[MCP] %s: %s not implemented (-32601) — '
                                 'peer is alive, trying next probe', name, meth)
                    with self._lock:
                        self._probe_method.pop(name, None)
                    continue
                if _is_peer_answered_error(e):
                    # Some other protocol-level error: still a completed round
                    # trip, so the transport is up. Don't reconnect.
                    logger.debug('[MCP] %s: %s returned a protocol error (%s) '
                                 '— peer alive', name, meth,
                                 _unwrap_exception_group(e))
                    return 'alive'
                # Timeout / dead pipe → genuinely unreachable.
                if isinstance(_unwrap_exception_group(e), TypeError):
                    # We called it wrong (signature drift across SDK majors).
                    # That says nothing about the peer — skip this candidate
                    # instead of declaring the server dead.
                    logger.warning('[MCP] Health probe %s() on %s is not '
                                   'zero-arg callable (%s) — skipping it',
                                   meth, name, _unwrap_exception_group(e))
                    with self._lock:
                        self._probe_method.pop(name, None)
                    continue
                logger.warning('[MCP] Health probe %s() on %s failed (%s) — '
                               'reconnecting', meth, name,
                               _unwrap_exception_group(e))
                return 'dead'

        # Every candidate answered -32601: the peer is talking, it just speaks
        # a method set we don't recognise. Alive, and NOT a reconnect trigger.
        logger.debug('[MCP] %s: all liveness probes answered -32601 — peer '
                     'alive but exposes no probe we know (%s)', name,
                     _unwrap_exception_group(last_exc) if last_exc else '?')
        return 'alive'

    async def _keepalive_loop(self) -> None:
        """Health-check every connected server periodically; reconnect dead ones.

        Runs on the MCP event-loop thread. The health verdict comes from
        ``_probe_liveness`` — "did the peer answer", NOT "did a ping succeed"
        (see that method for why the distinction is the whole point). The
        reconnect runs in a worker thread (``run_in_executor``) because
        ``_reconnect_server`` is a sync method that re-enters this very loop via
        ``run_coroutine_threadsafe`` — calling it inline would deadlock.

        A per-server circuit breaker (``_breaker``) gates reconnects: after a
        reconnect FAILS, that server is skipped until its exponentially
        growing backoff elapses, so a permanently-broken server isn't
        respawned every sweep. Servers whose handle was torn down by a failed
        reconnect are still revisited via the breaker keys + stored config, so
        they self-heal if the server eventually recovers.
        """
        stop = self._keepalive_stop
        while stop is not None and not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=MCP_KEEPALIVE_INTERVAL)
                break  # stop was set → exit loop
            except asyncio.TimeoutError:
                pass  # normal: interval elapsed, run a health sweep

            # Candidate set = live servers ∪ servers in backoff (the latter
            # may have no live handle after a failed reconnect).
            with self._lock:
                live = dict(self._servers)
                candidates = set(live) | set(self._breaker)
            loop = asyncio.get_running_loop()

            for name in candidates:
                # Skip servers whose backoff window hasn't elapsed.
                if self._breaker_blocks(name):
                    continue

                handle = live.get(name)
                session = handle.session if handle is not None else None

                if session is not None:
                    verdict = await self._probe_liveness(name, session)
                    if verdict == 'alive':
                        # Transport is alive — but the stored CREDENTIALS may
                        # have expired underneath it. Re-probe at most once per
                        # MCP_CRED_PROBE_INTERVAL (offloaded to a worker thread:
                        # _run_cred_probe drives call_tool, which re-enters this
                        # very loop and would deadlock if run inline).
                        #
                        # This hangs off "peer answered", NOT off a successful
                        # ping. Under the old code the credential probe was
                        # downstream of ping success, so a 2026-07-28 server
                        # (whose ping is -32601) stalled it FOREVER — an expired
                        # cookie would then never be surfaced. Measured before
                        # the fix: 0 credential probes across 12 sweeps.
                        if self._cred_probe_due(name):
                            loop.run_in_executor(None, self._run_cred_probe, name)
                        continue
                else:
                    # No live session but the breaker is tracking it (failed
                    # reconnect previously, backoff now elapsed) → retry.
                    logger.info('[MCP] Keepalive retrying backed-off server %s', name)

                try:
                    await loop.run_in_executor(None, self._reconnect_server, name)
                    logger.info('[MCP] Keepalive reconnected %s', name)
                except asyncio.CancelledError:
                    raise
                except Exception as re:
                    # _reconnect_server already recorded the breaker failure +
                    # logged the backoff; keep this terse to avoid double noise.
                    logger.debug('[MCP] Keepalive reconnect of %s still failing: %s',
                                 name, _unwrap_exception_group(re))
        logger.debug('[MCP] Keepalive loop exited')

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

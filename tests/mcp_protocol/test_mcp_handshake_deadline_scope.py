"""tests/mcp_protocol/test_mcp_handshake_deadline_scope.py — a handshake budget never becomes a call deadline.

Problem (observed 2026-10-30): every un-budgeted MCP call ended at exactly
30.0s — ``[MCP:Call] xuecheng.login TIMED OUT after 30.0s (budget=none)`` —
so a 90s 大象 CIBA login could never finish, long ``read_doc`` calls failed,
and the resulting streak tripped the degraded breaker for the whole server.

Root cause: the bridge passes the handshake budget (``MCP_CONNECT_TIMEOUT``) as
the session-level ``read_timeout_seconds`` so a silent server cannot hang
``initialize``. The MCP SDK applies that session value to every later request
whose own ``read_timeout_seconds`` is ``None`` (``ClientSession.send_request``
falls back to ``_session_read_timeout_seconds``). The bridge's documented
"None = no read timeout" call path therefore inherited the handshake deadline.

Rule (``tests/project_mod/test_no_backend_timeouts.py``): a HANDSHAKE may bound
itself; a WAIT may not. The connect path releases the session-level deadline
before publishing the session; an explicit per-server budget still applies.

Run:  pytest tests/mcp_protocol/test_mcp_handshake_deadline_scope.py -m unit
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit

HANDSHAKE_BUDGET_SECONDS = 0.2   # stands in for MCP_CONNECT_TIMEOUT
SLOWER_THAN_HANDSHAKE = 0.6


def _server(delay_seconds):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer('deadline-scope')

    @server.tool()
    async def slow() -> str:
        """Answer only after a delay longer than the simulated handshake budget."""
        await asyncio.sleep(delay_seconds)
        return 'slow-ok'

    return server


def _is_timeout(error):
    return isinstance(error, TimeoutError) or 'timed out' in str(error).lower()


def test_sdk_applies_the_session_read_timeout_to_unbudgeted_requests():
    """Premise pin. If a future SDK stops falling back to the session value,
    this fails and the release below can be re-evaluated."""
    from mcp import Client

    async def main():
        async with Client(_server(SLOWER_THAN_HANDSHAKE),
                          read_timeout_seconds=HANDSHAKE_BUDGET_SECONDS) as client:
            try:
                await client.call_tool('slow', {}, read_timeout_seconds=None)
            except Exception as error:  # noqa: BLE001 - the SDK error type is the evidence
                return error
            return None

    error = asyncio.run(main())
    assert error is not None and _is_timeout(error), (
        'the SDK no longer applies the session-level read timeout to a request '
        f'without its own budget (got {error!r})')


def test_unbudgeted_bridge_call_outlives_the_handshake_budget():
    """THE headline: after the connect path's release, a call with no budget
    waits for the real answer instead of dying at the handshake deadline."""
    from mcp import Client

    import lib.mcp.client as mc
    from lib.mcp.client._bridge import _release_handshake_read_deadline

    async def main():
        async with Client(_server(SLOWER_THAN_HANDSHAKE),
                          read_timeout_seconds=HANDSHAKE_BUDGET_SECONDS) as client:
            assert _release_handshake_read_deadline(client) is True
            bridge = mc.MCPBridge()
            handle = mc._MCPServerHandle('deadline-scope', {})
            handle.session = client
            handle.sdk_generation = 2
            return await bridge._async_call_tool(handle, 'slow', {}, None)

    assert 'slow-ok' in str(asyncio.run(main()))


def test_explicit_per_server_budget_still_bounds_the_call():
    """Reverse direction: releasing the handshake deadline must not remove a
    deliberate per-server ``timeout`` from mcp_servers.json."""
    from mcp import Client

    import lib.mcp.client as mc
    from lib.mcp.client._bridge import _release_handshake_read_deadline

    async def main():
        async with Client(_server(1.6), read_timeout_seconds=HANDSHAKE_BUDGET_SECONDS) as client:
            _release_handshake_read_deadline(client)
            bridge = mc.MCPBridge()
            handle = mc._MCPServerHandle('deadline-scope', {'timeout': 1})
            handle.session = client
            handle.sdk_generation = 2
            try:
                await bridge._async_call_tool(handle, 'slow', {}, 1)
            except Exception as error:  # noqa: BLE001 - the SDK error type is the evidence
                return error
            return None

    error = asyncio.run(main())
    assert error is not None and _is_timeout(error), \
        f'an explicit 1s budget no longer bounds the call (got {error!r})'


def test_connected_session_can_finish_a_call_longer_than_its_handshake(monkeypatch):
    """Exercise the real connection owner and SDK with an in-process transport."""
    from mcp.client import sse
    from lib.mcp.client import _bridge

    server = _server(SLOWER_THAN_HANDSHAKE)
    # Replace only transport construction. The SDK still owns negotiation,
    # requests and shutdown against the real in-process MCP server.
    monkeypatch.setattr(sse, 'sse_client', lambda url, headers, **options: server)
    monkeypatch.setattr(_bridge, 'MCP_CONNECT_TIMEOUT', HANDSHAKE_BUDGET_SECONDS)

    async def main():
        bridge = _bridge.MCPBridge()
        handle, tools = await bridge._async_start_owner('deadline-scope', {
            'transport': 'sse', 'url': 'http://in-process.invalid/sse',
        })
        try:
            assert [tool.name for tool in tools] == ['slow']
            return await bridge._async_call_tool(handle, 'slow', {}, None)
        finally:
            await bridge._async_signal_shutdown(handle)
            await handle._owner_task

    assert 'slow-ok' in str(asyncio.run(main()))

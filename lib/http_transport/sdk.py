"""SDK composition ports; SDK defaults remain owned by each upstream client.

Network routing and transport cleanup use the same managed factories as Tofu
HTTP. Import SDKs lazily so optional integrations do not tax application boot.
"""
from __future__ import annotations


def mcp_http_client(headers=None, timeout=None, auth=None):
    """MCP's client-factory protocol, including its httpx2 transport types."""
    from mcp.shared import _httpx_utils as sdk
    from lib.http_transport.outbound_async import managed_async_client

    http = getattr(sdk, 'httpx2', None) or sdk.httpx
    timeout = timeout or http.Timeout(
        sdk.MCP_DEFAULT_TIMEOUT, read=sdk.MCP_DEFAULT_SSE_READ_TIMEOUT)
    return managed_async_client(
        http_module=http, headers=headers, timeout=timeout, auth=auth,
        follow_redirects=True)

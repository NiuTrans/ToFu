"""routes/api_v1/tools.py — Live tool-registry inventory surface.

One read-only endpoint backing the Settings → 工具 panel (and any headless
client that wants the full picture rather than ``/api/v1/capabilities``'
hand-maintained 5-group summary):

  GET /api/v1/tools — every tool family registered in this process, grouped
  by category. The Settings page treats it as a process-global catalogue;
  request-local gate metadata remains in the payload for headless diagnostics
  but is deliberately not presented as a conversation state.

Auth follows the other user-facing GET surfaces (skills/memory/mcp):
``@require_auth`` — a cookie session or any bearer token passes. The payload
is read-only metadata (names, descriptions, gate state); it exposes no
secrets, no config values, and no per-tenant data. Deliberately NOT
``public=True`` (unlike /capabilities): this endpoint enumerates the full
registered surface including plugin names — operator-visible information
that an unauthenticated probe on a public deployment has no need for.

Uncached on purpose: the panel's promise is "what is registered RIGHT NOW"
— an MCP reconnect or a plugin install must show up on the next open.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import request_user_id, require_auth

logger = get_logger(__name__)

api_v1_tools_bp = Blueprint('api_v1_tools', __name__)


@api_v1_tools_bp.route('/api/v1/tools', methods=['GET'])
@require_auth
@api_meta(
    summary='Live tool-registry inventory',
    description=(
        'Every tool family registered in this process (built-in + plugin '
        'specs + connected MCP servers), grouped by category, with tool rows '
        '(name, description, required params, and write/handler metadata). '
        'The payload scope is ``global_registry``: registered families and '
        'tools are always listed. ``gate`` / ``gate_state`` are retained only '
        'as reference-context diagnostics and are not global availability. '
        'Computed fresh per call from the registry SSOT '
        '(``lib.tools.registry``); uncached so it always reflects the live '
        'process state.'
    ),
    tags=['tools'],
)
def list_tools_v1():
    from lib.tools.registry._introspect import build_tool_inventory
    response, status = api_ok(build_tool_inventory(
        owner_user_id=int(request_user_id())))
    # The catalogue promises a fresh process snapshot on every open/refresh.
    # Make that true across browsers and reverse proxies, not merely inside the
    # Python builder (which is already intentionally uncached).
    response.headers['Cache-Control'] = 'private, no-store'
    return response, status


__all__ = ['api_v1_tools_bp']

"""routes/api_v1/tools.py — Live tool-registry inventory surface.

One read-only endpoint backing the Settings → 工具 panel (and any headless
client that wants the full picture rather than ``/api/v1/capabilities``'
hand-maintained 5-group summary):

  GET /api/v1/tools — every tool family registered in this process, grouped
  by category, each carrying its gate state evaluated live by the family's
  own ``build()`` (via ``lib.tools.registry._introspect.build_tool_inventory``).

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

from flask import Blueprint

from lib.api_response import api_ok
from lib.log import get_logger
from lib.openapi import api_meta

from .auth import require_auth

logger = get_logger(__name__)

api_v1_tools_bp = Blueprint('api_v1_tools', __name__)


@api_v1_tools_bp.route('/api/v1/tools', methods=['GET'])
@require_auth
@api_meta(
    summary='Live tool-registry inventory',
    description=(
        'Every tool family registered in this process (built-in + plugin '
        'specs + connected MCP servers), grouped by category. Each family '
        'carries ``gate`` (the human-readable switch), ``gate_state`` '
        '(on/off/standby/error under the reference context — server '
        'defaults, no project attached) and its tool rows (name, '
        'description, required params, write/handler badges, enabled). '
        'Computed fresh per call from the registry SSOT '
        '(``lib.tools.registry``); uncached so it always reflects the live '
        'process state.'
    ),
    tags=['tools'],
)
def list_tools_v1():
    from lib.tools.registry._introspect import build_tool_inventory
    return api_ok(build_tool_inventory())


__all__ = ['api_v1_tools_bp']

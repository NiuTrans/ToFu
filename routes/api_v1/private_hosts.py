"""routes/api_v1/private_hosts.py — Internal-host SSRF allowlist management.

These endpoints let the user name the internal hosts the server-side fetch
pipeline is allowed to reach. By default any host resolving to a private /
loopback / reserved address is blocked (SSRF guard); an entry here is the
explicit statement "I do mean to fetch this one".

Routes:
  GET    /api/v1/private-hosts               — list allowlist entries
  POST   /api/v1/private-hosts               — create/update one (host/label/enabled)
  POST   /api/v1/private-hosts/<host>/toggle — enable/disable
  DELETE /api/v1/private-hosts/<host>        — remove

This is the REACHABILITY gate and nothing else. It stores no credentials and
returns none: an entry here does NOT log the server in anywhere. Its sibling
``/api/v1/auth-sources`` is the IDENTITY gate (cookies) and confers no SSRF
exemption. The two are kept separate on purpose — see ``lib/private_hosts.py``.

Hosts are matched by NAME (exact or parent-suffix), never by resolved IP, so a
rotating internal load balancer cannot silently fall out of the allowlist. Bare
IP addresses are rejected with 400 rather than stored.
"""

from __future__ import annotations

from quart import Blueprint

from lib.api_response import api_bad_request, api_error, api_ok
from lib.log import get_logger
from lib.openapi import api_meta
from lib.request_parser import parse_body

from .auth import require_auth

logger = get_logger(__name__)

api_v1_private_hosts_bp = Blueprint('api_v1_private_hosts', __name__)


@api_v1_private_hosts_bp.route('/api/v1/private-hosts', methods=['GET'])
@require_auth
@api_meta(
    summary='List internal-host allowlist entries',
    description=(
        'Returns ``{hosts: [...]}`` with ``host`` / ``label`` / ``enabled`` / '
        '``updated_at``. Nothing is redacted — a hostname is not a secret. '
        'Only ENABLED entries are exempted from the SSRF guard.'
    ),
    tags=['capabilities'],
)
def list_private_hosts():
    from lib.private_hosts import list_hosts
    return api_ok({'hosts': list_hosts()})


@api_v1_private_hosts_bp.route('/api/v1/private-hosts', methods=['POST'])
@require_auth
@api_meta(
    summary='Create or update an internal-host allowlist entry',
    description=(
        'Body: ``{host, label?, enabled?}``. ``host`` is normalized — a pasted '
        'URL, port, uppercase or trailing dot are all accepted and reduced to '
        'the bare hostname. A BARE IP ADDRESS is rejected with 400: internal '
        'load balancers rotate their address between lookups, so an IP entry '
        'silently stops matching. New entries default to enabled. Takes effect '
        'immediately — no restart.'
    ),
    tags=['capabilities'],
)
def upsert_private_host():
    from lib.private_hosts import upsert_host

    data = parse_body()
    host = (data.get('host') or '').strip()
    if not host:
        return api_bad_request('host is required', field='host')
    try:
        row = upsert_host(host, label=data.get('label'), enabled=data.get('enabled'))
    except ValueError as e:
        return api_bad_request(str(e), field='host')
    _resync()
    return api_ok({'host': row})


@api_v1_private_hosts_bp.route('/api/v1/private-hosts/<host>/toggle', methods=['POST'])
@require_auth
@api_meta(summary='Enable/disable an internal-host allowlist entry', tags=['capabilities'])
def toggle_private_host(host):
    from lib.private_hosts import set_enabled

    data = parse_body()
    enabled = bool(data.get('enabled', True))
    if not set_enabled(host, enabled):
        return api_error(f'Unknown host: {host}', status=404)
    _resync()
    return api_ok({'host': host, 'enabled': enabled})


@api_v1_private_hosts_bp.route('/api/v1/private-hosts/<host>', methods=['DELETE'])
@require_auth
@api_meta(
    summary='Remove an internal-host allowlist entry',
    description='The host becomes unreachable again immediately (SSRF guard re-applies).',
    tags=['capabilities'],
)
def delete_private_host(host):
    from lib.private_hosts import delete_host
    if not delete_host(host):
        return api_error(f'Unknown host: {host}', status=404)
    _resync()
    return api_ok({'host': host})


def _resync():
    """Push the updated allowlist into tofu-search immediately.

    Without this the change would only land on the next config reload or
    restart, and the user would reasonably conclude the setting is broken.
    """
    try:
        from lib.search_bridge import sync_search_config
        sync_search_config()
    except Exception as e:
        logger.warning('[PrivHosts] search-config resync failed: %s', e)

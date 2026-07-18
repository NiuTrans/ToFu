# HOT_PATH
"""Endpoint / header helpers for the outbound Anthropic Messages adapter.

Holds ``ANTHROPIC_VERSION`` plus the two seam functions that resolve the
Messages endpoint from a provider base URL and build the auth headers. No
dependency on the body-conversion modules — this is a leaf.
"""

from lib.log import get_logger

logger = get_logger(__name__)

ANTHROPIC_VERSION = '2023-06-01'


def anthropic_messages_url(base_url: str) -> str:
    """Resolve the Messages endpoint from a provider base URL.

    Handles the three base-URL shapes we ship:
      * already a Messages endpoint (``…/messages``)        → used as-is
      * ends at the API version segment (``…/v1``,          → ``+ /messages``
        e.g. direct ``https://api.anthropic.com/v1``)
      * ends at a gateway root (``…/v1/anthropic``)         → ``+ /v1/messages``
    """
    u = (base_url or '').rstrip('/')
    if u.endswith('/messages'):
        return u
    if u.endswith('/v1'):
        return u + '/messages'
    return u + '/v1/messages'


def anthropic_headers(api_key: str, extra_headers: dict = None) -> dict:
    """Build Anthropic auth headers. Sends both ``x-api-key`` and a Bearer
    token (gateways differ on which they read), plus ``anthropic-version``."""
    hdrs = {
        'Content-Type': 'application/json',
        'anthropic-version': ANTHROPIC_VERSION,
    }
    if api_key:
        hdrs['x-api-key'] = api_key
        hdrs['Authorization'] = f'Bearer {api_key}'
    if extra_headers:
        hdrs.update(extra_headers)
    return hdrs

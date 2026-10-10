#!/usr/bin/env python3
"""lib/desktop_agent/_probe.py — does this URL actually reach Tofu?

The connect line's address half is minted from whatever URL the USER's
browser happened to reach the panel on. Under an SSO-fronted gateway
(cloud-IDE preview proxy, corporate IdP) that address answers every
request itself — the agent's polls are bounced at the edge and never
reach Tofu, and the user is left staring at a dead toggle with no
explanation (owner incident 2026-08-03: a codelab proxy URL answered
``401 {"error":"Unauthorized"}`` to everything, access.log showed zero
agent polls).

``GET /api/health`` is the one open endpoint, so a single round-trip
settles it — asked at paste time, not discovered after hours of silent
retrying.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _is_route_url(url: str) -> bool:
    """Accept HTTP base URLs without embedded credentials or URL suffixes."""
    from urllib.parse import urlsplit
    try:
        parts = urlsplit(url)
        return (parts.scheme in ('http', 'https') and bool(parts.hostname)
                and parts.port != 0 and parts.username is None
                and parts.password is None and not parts.query
                and not parts.fragment
                and not any(c.isspace() or ord(c) < 32 for c in url)
                and '\\' not in url)
    except (ValueError, TypeError):
        return False


def probe_server(url: str, timeout: float = 4.0) -> tuple[bool, str]:
    """Verify ``url`` is answered by Tofu. Returns ``(ok, reason)``.

    ``reason`` is '' on success, else one of:
      * ``http_401`` / ``http_403`` — something (very likely an SSO
        gateway, not Tofu) refused the request;
      * ``http_<n>`` — any other HTTP status;
      * ``not_tofu`` — a 200 that is not Tofu's health JSON (a gateway's
        landing page masquerading as the server);
      * ``timeout`` / ``unreachable`` / ``error`` — transport failures.
    """
    import requests
    base = (url or '').strip().rstrip('/')
    if not base or not _is_route_url(base):
        return False, 'unreachable'
    try:
        # Honor Requests' proxy/NO_PROXY and CA-bundle configuration. Health
        # is only a reachability hint, never proof of server identity. Do not
        # follow a gateway redirect or attach ambient .netrc credentials to
        # a URL that may have come from unauthenticated LAN discovery.
        resp = requests.get(base + '/api/health', timeout=timeout,
                            allow_redirects=False, auth=lambda request: request)
    except requests.exceptions.Timeout as e:
        logger.debug('[Agent] server probe timed out: %s', e)
        return False, 'timeout'
    except requests.exceptions.ConnectionError as e:
        logger.debug('[Agent] server probe unreachable: %s', e)
        return False, 'unreachable'
    except requests.RequestException as e:
        logger.debug('[Agent] server probe failed: %s', e)
        return False, 'error'
    if resp.status_code in (401, 403):
        return False, 'http_%d' % resp.status_code
    if resp.status_code != 200:
        return False, 'http_%d' % resp.status_code
    try:
        body = resp.json()
    except ValueError as e:
        logger.debug('[Agent] server probe response not JSON: %s', e)
        return False, 'not_tofu'
    if not isinstance(body, dict) or not body.get('bootId'):
        return False, 'not_tofu'
    return True, ''


def is_tofu_error_envelope(obj) -> bool:
    """Whether a parsed JSON body is TOFU's api_error envelope.

    The poll loop's 401 handler must tell two refusals apart: Tofu's own
    401 (the bridge secret is wrong — fixable with a fresh connect line)
    versus a gateway's 401. The bridge adapter returns the flat
    ``{"error": "bridge_auth_required"}`` contract; other API boundaries
    use ``{"ok": false, "error": {...}}``. A generic string error such as
    ``Unauthorized`` is not the bridge contract. This is diagnostic
    classification only: an error body cannot authenticate a server.
    """
    return (isinstance(obj, dict)
            and (obj.get('error') == 'bridge_auth_required'
                 or (obj.get('ok') is False
                     and isinstance(obj.get('error'), dict))))

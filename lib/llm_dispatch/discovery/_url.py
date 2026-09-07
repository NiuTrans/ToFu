"""lib/llm_dispatch/discovery/_url.py — URL normalization + local-endpoint helpers.

Holds the shared, lazily-populated ``_LOCAL_CIDRS_CACHE`` (mutable module
state) alongside its only reader/writer (``_local_cidrs`` /
``_parse_local_cidrs``) so the cache is shared by reference across the
package.
"""

import ipaddress
import os
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════
#  Local-endpoint Helpers
# ══════════════════════════════════════════════════════

# Common chat/completion suffixes users paste — we strip these to recover
# the OpenAI-compatible base URL that hosts /models.
_CHAT_SUFFIXES = (
    '/chat/completions',
    '/completions',
    '/embeddings',
)


def normalize_base_url(url: str) -> str:
    """Strip well-known chat/completion suffixes so /models lookup succeeds.

    Users frequently paste the full chat-completions URL (e.g.
    ``http://10.0.0.5:8080/v1/chat/completions``) when they mean the
    base URL (``http://10.0.0.5:8080/v1``). Normalizing here keeps the
    UI forgiving without changing the wire protocol.
    """
    if not url:
        return url
    cleaned = url.rstrip('/ ')
    lower = cleaned.lower()
    for suffix in _CHAT_SUFFIXES:
        if lower.endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
            break
    return cleaned.rstrip('/')


def _parse_local_cidrs() -> list:
    """Parse ``TOFU_LOCAL_CIDRS`` (comma-separated CIDR list) once.

    Lets operators tag internal-but-publicly-routable IP ranges (e.g. a
    corp datacenter using 33.0.0.0/8) as 'local' so the health checker
    polls them and the UI brands them correctly.  Empty by default.
    """
    raw = os.environ.get('TOFU_LOCAL_CIDRS', '').strip()
    if not raw:
        return []
    nets = []
    for tok in raw.split(','):
        tok = tok.strip()
        if not tok:
            continue
        try:
            nets.append(ipaddress.ip_network(tok, strict=False))
        except ValueError as e:
            logger.warning('[BrandDetect] Invalid TOFU_LOCAL_CIDRS entry %r: %s', tok, e)
    return nets


_LOCAL_CIDRS_CACHE = None


def _local_cidrs() -> list:
    global _LOCAL_CIDRS_CACHE
    if _LOCAL_CIDRS_CACHE is None:
        _LOCAL_CIDRS_CACHE = _parse_local_cidrs()
    return _LOCAL_CIDRS_CACHE


def is_local_endpoint(base_url: str) -> bool:
    """True when the URL points at a private / loopback / .local host.

    Used to tag self-hosted vLLM / SGLang / Ollama endpoints so the
    health-checker only polls them and the UI groups them as 'local'.

    Also returns True for IPs in any CIDR listed in the
    ``TOFU_LOCAL_CIDRS`` env var (operator escape-hatch for internal
    deployments on RFC-public IP space).
    """
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or '').lower()
    except Exception as e:
        logger.debug('[BrandDetect] Failed to parse URL %s: %s', base_url, e)
        return False
    if not host:
        return False
    if host in ('localhost',) or host.endswith(('.local', '.internal', '.lan', '.intranet')):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as _e_audit:
        logger.debug('[discovery] is_local_endpoint caught %s: %s', type(_e_audit).__name__, _e_audit)
        return False
    if ip.is_private or ip.is_loopback or ip.is_link_local:
        return True
    for net in _local_cidrs():
        if ip in net:
            return True
    return False


def is_raw_ip_host(base_url: str) -> bool:
    """True when *base_url*'s host is a bare IPv4/IPv6 literal (not a domain)."""
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or '').lower()
    except Exception as e:
        logger.debug('[BrandDetect] Failed to parse URL %s: %s', base_url, e)
        return False
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError as e:
        logger.debug('[BrandDetect] %s is not a raw IP host: %s', host, e)
        return False


def should_bypass_proxy(base_url: str) -> bool:
    """True when traffic to *base_url* should bypass the corporate HTTP proxy.

    Self-hosted LLM endpoints are reached directly, never through the corp
    proxy (which only routes to the public internet). This is broader than
    :func:`is_local_endpoint` on purpose — it also covers any bare IP
    literal, because a raw-IP base URL is in practice always a self-hosted /
    internal box (commercial APIs are addressed by domain). That includes
    internal-but-publicly-routable corp ranges (e.g. ``33.x``) which
    :func:`is_local_endpoint` cannot classify without ``TOFU_LOCAL_CIDRS``.
    """
    return is_local_endpoint(base_url) or is_raw_ip_host(base_url)

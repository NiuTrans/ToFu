"""lib/byo_egress.py — SSRF egress guard for BYO provider base URLs.

Third parties register their own LLM endpoints (``base_url`` + ``api_key``)
and the server then makes outbound requests to that URL — model discovery
(``GET /v1/models``), balance probing, and proxying every chat completion.
Without a guard this is a server-side request forgery surface: a caller
could point ``base_url`` at the cloud-metadata service
(``169.254.169.254``), a loopback admin port, or use it to port-scan the
internal network and read the responses back (a blind-SSRF oracle).

Design tension
--------------
BYO providers are *frequently and legitimately* self-hosted on private
networks — a vLLM / SGLang / Ollama box on ``10.x`` or ``127.0.0.1`` is a
first-class, documented use case. A blanket "block all private IPs" rule
would break the feature for its primary audience. So the policy is:

* **Always deny** the genuinely dangerous targets regardless of config:
  link-local (``169.254.0.0/16`` — AWS/GCP/Azure metadata lives here),
  multicast, reserved, and unspecified (``0.0.0.0``) ranges.
* **Loopback** (``127.0.0.0/8``, ``::1``) and **RFC1918 / ULA private**
  ranges are allowed BY DEFAULT (self-hosted LLM case) but can be locked
  down by operators running a multi-tenant relay:
  - ``TOFU_BYO_BLOCK_LOOPBACK=1`` — refuse loopback targets.
  - ``TOFU_BYO_BLOCK_PRIVATE=1``  — refuse RFC1918 / ULA targets too
    (recommended for any deployment that accepts BYO providers from
    untrusted third parties).
* ``TOFU_BYO_ALLOW_HOSTS`` — comma-separated exact hostnames that bypass
  the IP checks entirely (an explicit allow-list escape hatch).

Like ``routes/upload.py::_safe_image_fetch``, the host is resolved to
*every* candidate IP (``getaddrinfo``) and each is checked, so a
single-record DNS-rebinding trick can't slip a blocked address through.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['EgressDenied', 'validate_egress_url', 'is_egress_allowed']


class EgressDenied(ValueError):
    """Raised when a BYO base_url targets a forbidden address."""


def _flag(name: str) -> bool:
    return os.environ.get(name, '').strip().lower() in ('1', 'true', 'yes', 'on')


def _allow_hosts() -> set[str]:
    raw = (os.environ.get('TOFU_BYO_ALLOW_HOSTS') or '').strip()
    if not raw:
        return set()
    return {h.strip().lower() for h in raw.split(',') if h.strip()}


def _ip_verdict(ip_str: str) -> str | None:
    """Return a denial reason for ``ip_str``, or None when allowed.

    Honors the ``TOFU_BYO_BLOCK_LOOPBACK`` / ``TOFU_BYO_BLOCK_PRIVATE``
    operator toggles. Link-local / multicast / reserved / unspecified are
    ALWAYS denied (the metadata-SSRF class).
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return 'unparseable address'
    # IPv4-mapped IPv6 (::ffff:a.b.c.d) — judge by the embedded v4 addr.
    mapped = getattr(ip, 'ipv4_mapped', None)
    if mapped is not None:
        ip = mapped
    # Always-deny ranges (the dangerous SSRF targets).
    if ip.is_link_local:           # 169.254/16 + fe80::/10 → cloud metadata
        return 'link-local (cloud-metadata range)'
    if ip.is_multicast:
        return 'multicast'
    if ip.is_reserved:
        return 'reserved'
    if ip.is_unspecified:          # 0.0.0.0 / ::
        return 'unspecified'
    # Config-gated ranges.
    if ip.is_loopback and _flag('TOFU_BYO_BLOCK_LOOPBACK'):
        return 'loopback (blocked by TOFU_BYO_BLOCK_LOOPBACK)'
    if (ip.is_private and not ip.is_loopback
            and _flag('TOFU_BYO_BLOCK_PRIVATE')):
        return 'private (blocked by TOFU_BYO_BLOCK_PRIVATE)'
    return None


def is_egress_allowed(url: str) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a BYO ``base_url`` without raising.

    ``reason`` is '' when allowed, else a human-readable denial cause.
    """
    parsed = urlparse(url or '')
    if parsed.scheme not in ('http', 'https'):
        return False, f'unsupported scheme {parsed.scheme!r} (http/https only)'
    host = (parsed.hostname or '').lower()
    if not host:
        return False, 'missing hostname'
    if host in _allow_hosts():
        return True, ''
    # Resolve every candidate IP (defeats single-record DNS rebinding).
    try:
        infos = socket.getaddrinfo(host, parsed.port or None,
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f'DNS resolution failed: {e}'
    if not infos:
        return False, 'DNS returned no addresses'
    for info in infos:
        ip_str = info[4][0]
        verdict = _ip_verdict(ip_str)
        if verdict:
            return False, f'host {host!r} resolves to blocked IP {ip_str} ({verdict})'
    return True, ''


def validate_egress_url(url: str) -> None:
    """Raise :class:`EgressDenied` if ``url`` is not a permitted egress target.

    Call this at BYO-provider registration time AND before each outbound
    probe/proxy (DNS can change between registration and use, so the
    use-time check is the security-critical one).
    """
    allowed, reason = is_egress_allowed(url)
    if not allowed:
        logger.warning('[BYOEgress] denied base_url %r: %s', url, reason)
        raise EgressDenied(
            f'base_url is not an allowed request target: {reason}. '
            f'Self-hosted endpoints on private IPs are permitted by default; '
            f'cloud-metadata / link-local addresses are always blocked. '
            f'Operators can adjust via TOFU_BYO_BLOCK_PRIVATE / '
            f'TOFU_BYO_ALLOW_HOSTS.')

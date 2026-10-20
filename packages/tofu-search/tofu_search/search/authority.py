"""URL-derived source hints shared by ranking and model-facing formatting.

Entry points: classify_authority, authority_label, and host_brand_labels.
Only standard-library URL parsing is used; no network or ownership lookup.

Query/host token matches, known reference hosts, documentation-shaped hosts,
and aggregator-like domain names are heuristics. None establishes who owns a
page, whether it is a primary source for this query, or whether its claims are
true. Shared publishing hosts can carry arbitrary third-party material.

AUTHORITY_BOOST contains adjustment ceilings, not independent relevance
evidence. The reranker bounds their contribution by actual lexical evidence.
"""

from __future__ import annotations

import re
from ipaddress import ip_address
from urllib.parse import urlparse

__all__ = [
    "AUTHORITY_BOOST",
    "classify_authority",
    "authority_label",
    "brand_tokens",
    "host_brand_labels",
]

# Preserve useful source preferences among comparable results. The reranker
# caps positive contributions at the lexical score and negative contributions
# at half of it: URL hints cannot create relevance or erase all its evidence.
AUTHORITY_BOOST = {
    "query_match": 2.5,
    "reference_host": 1.2,
    "neutral": 0.0,
    "aggregator_hint": -1.5,
}

# Keep uncertainty in each label so it survives persisted/compacted results.
_LABEL = {
    "query_match": "Unverified — query/host token match",
    "reference_host": "Unverified — reference/documentation host heuristic",
    "neutral": "",
    "aggregator_hint": "Unverified — aggregator-like domain name",
}

# Reference/publishing host hints, matched at DNS label boundaries. Inclusion
# is not an endorsement of pages, packages, repositories, or authors they host.
_REFERENCE_HOST_SUFFIXES = (
    ".gov", ".edu", ".mil", ".int",
    "python.org", "pypi.org", "readthedocs.io", "readthedocs.org",
    "arxiv.org", "github.com", "github.io", "gitlab.com",
    "nist.gov", "nvd.nist.gov", "mitre.org",
    "developer.mozilla.org", "mozilla.org",
    "w3.org", "ietf.org", "rfc-editor.org", "iana.org", "whatwg.org",
    "kernel.org", "postgresql.org", "sqlite.org", "nodejs.org",
    "kubernetes.io", "docker.com", "npmjs.com",
)

# Documentation-shaped subdomains are only a structural hint.
_DOCUMENTATION_SUBDOMAIN_PREFIXES = ("docs.", "developer.", "dev.", "api.", "learn.")

# SEO / price-aggregator / listicle tells in the second-level domain.
_AGGREGATOR_SLD_RE = re.compile(
    r"(cost|compare|comparison|deals?|pricing|cheap|coupon|bestof|top\d|"
    r"vsbattle|alternativeto|reviews?)"
)

# Registrars/CDNs whose bare SLD is meaningless as a brand token.
_GENERIC_SLD = frozenset({
    "com", "org", "net", "io", "co", "dev", "ai", "app", "info", "www",
    "amazonaws", "cloudfront", "github", "medium", "substack", "blogspot",
    "wordpress", "wikipedia", "quora", "reddit", "stackexchange",
})

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _host(url: str) -> str:
    """Normalize an HTTP URL's DNS hostname; reject malformed and IP hosts."""
    try:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return ""
        _ = parsed.port  # Validate a supplied port rather than ignoring malformed URLs.
        host = (parsed.hostname or "").removesuffix(".").encode("idna").decode("ascii").lower()
        if len(host) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            return ""
        try:
            ip_address(host)
        except ValueError:
            return host
        return ""
    except (ValueError, UnicodeError):
        return ""


def _registrable_sld(host: str) -> str:
    """Best-effort registrable brand token (the SLD).

    ``aws.amazon.com`` → ``amazon``; ``cloudflare.com`` → ``cloudflare``;
    ``docs.astral.sh`` → ``astral``. Handles common two-level public suffixes
    (``co.uk``, ``com.cn`` …) so we don't return the suffix as the brand.
    """
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    _TWO_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if len(parts) >= 3 and parts[-2] in _TWO_LEVEL and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2]


def brand_tokens(text: str) -> set[str]:
    """Lowercase alphanumeric tokens of length ≥ 3 (query brand candidates)."""
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) >= 3}


def host_brand_labels(url: str) -> set[str]:
    """Non-generic URL tokens for heuristic query matching and diversification.

    ``aws.amazon.com`` yields ``{aws, amazon}``; ``www.cloudflare.com`` yields
    ``{cloudflare}``. These are spelling matches, not verified brand identities.
    Subdomain names can be chosen by unrelated publishers or hosting tenants.
    """
    host = _host(url)
    if not host:
        return set()
    return {p for p in host.split(".") if len(p) >= 3} - _GENERIC_SLD


def classify_authority(url: str, query: str = "") -> str:
    """Classify a URL hint without certifying page authority or ownership."""
    host = _host(url)
    if not host:
        return "neutral"

    sld = _registrable_sld(host)

    # Matching a query word in a hostname is useful but does not authenticate
    # a vendor. Generic hosting labels remain excluded from this weak signal.
    q_tokens = brand_tokens(query)
    if host_brand_labels(url) & q_tokens:
        return "query_match"

    if any(host == s.lstrip(".") or host.endswith("." + s.lstrip("."))
           for s in _REFERENCE_HOST_SUFFIXES):
        return "reference_host"
    if any(host.startswith(p) for p in _DOCUMENTATION_SUBDOMAIN_PREFIXES):
        return "reference_host"

    # Domain wording alone cannot establish that a site is an aggregator.
    if _AGGREGATOR_SLD_RE.search(sld):
        return "aggregator_hint"

    return "neutral"


def authority_label(url: str, query: str = "") -> str:
    """Human-readable authority tag for format.py (empty string if neutral)."""
    return _LABEL[classify_authority(url, query)]

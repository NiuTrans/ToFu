"""tofu_search/search/authority.py — primary-source domain authority.

Motivation
----------
Reranking on BM25 relevance alone lets an SEO aggregator/blogspam page
(e.g. ``latencycost.com`` reciting third-hand pricing) outrank the primary
source (the vendor's own ``aws.amazon.com`` pricing page), so the model
synthesizes wrong numbers from a weaker source. We add a light,
domain-authority signal that PREFERS primary sources when relevance is
otherwise comparable.

Design (deliberately general, NOT a per-eval allowlist)
-------------------------------------------------------
Authority is derived from the URL host with three signals, in priority order:

  OFFICIAL (highest)  — the host's registrable brand token matches a
      brand token IN THE QUERY. This is the key generalizable rule: a query
      that names "aws", "cloudflare", "react", "postgresql", … lifts THAT
      vendor's own domain (``aws.amazon.com``, ``cloudflare.com``,
      ``react.dev``, ``postgresql.org``) above third parties writing about it.
      No hardcoded vendor list — it keys off whatever the query mentions.

  PRIMARY   — structurally primary/authoritative hosts independent of the
      query: standards bodies & official docs/registries (``*.gov``,
      ``*.edu``, docs/developer subdomains, ``python.org``, ``pypi.org``,
      ``arxiv.org``, ``github.com``, ``nvd.nist.gov``, MDN, …). These are
      trustworthy primary references regardless of topic.

  AGGREGATOR (lowest) — hosts that look like SEO/price-aggregator/listicle
      farms (``*cost*``, ``*compare*``, ``*deals*``, ``*pricing*`` in the
      SLD, etc.). Demoted, never dropped — the model still sees them.

  NEUTRAL   — everything else (score 0).

The classifier returns a small additive BM25 boost so it only breaks ties /
nudges ordering; it never overwhelms a strong lexical match.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

__all__ = [
    "AUTHORITY_BOOST",
    "classify_authority",
    "authority_label",
    "brand_tokens",
    "host_brand_labels",
]

# Additive BM25 boosts per tier. BM25 term scores on these docs are typically
# ~1–8; these are intentionally modest so authority nudges ordering / breaks
# near-ties rather than steamrolling a much more relevant page.
AUTHORITY_BOOST = {
    "official": 2.5,
    "primary": 1.2,
    "neutral": 0.0,
    "aggregator": -1.5,
}

# Human-readable tag surfaced to the model in format.py.
_LABEL = {
    "official": "OFFICIAL SOURCE (vendor/authoritative domain named in query)",
    "primary": "PRIMARY SOURCE (official docs / standards / registry)",
    "neutral": "",
    "aggregator": "third-party aggregator — verify against a primary source",
}

# Structurally-primary hosts, independent of the query topic. Matched as
# suffix (``endswith``) against the registrable host so subdomains count.
_PRIMARY_HOST_SUFFIXES = (
    ".gov", ".edu", ".mil", ".int",
    "python.org", "pypi.org", "readthedocs.io", "readthedocs.org",
    "arxiv.org", "github.com", "github.io", "gitlab.com",
    "nist.gov", "nvd.nist.gov", "mitre.org",
    "developer.mozilla.org", "mozilla.org",
    "w3.org", "ietf.org", "rfc-editor.org", "iana.org", "whatwg.org",
    "kernel.org", "postgresql.org", "sqlite.org", "nodejs.org",
    "kubernetes.io", "docker.com", "npmjs.com",
)

# Doc/developer subdomains are primary regardless of the parent brand.
_PRIMARY_SUBDOMAIN_PREFIXES = ("docs.", "developer.", "dev.", "api.", "learn.")

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
    try:
        return (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]
    except Exception:
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
    """Non-generic host labels usable as a brand identity for ``url``.

    ``aws.amazon.com`` → ``{amazon}`` (``aws``<3? no, len 3 → kept; ``com``
    generic → dropped); ``www.cloudflare.com`` → ``{cloudflare}``. Registrar/
    CDN/generic labels (``com``/``amazonaws``/``github``/…) are excluded so a
    hosting domain is not mistaken for the entity it hosts. Shared by
    ``classify_authority`` (OFFICIAL detection) and rerank entity attribution.
    """
    host = _host(url)
    if not host:
        return set()
    return {p for p in host.split(".") if len(p) >= 3} - _GENERIC_SLD


def classify_authority(url: str, query: str = "") -> str:
    """Return one of 'official' | 'primary' | 'aggregator' | 'neutral'."""
    host = _host(url)
    if not host:
        return "neutral"

    sld = _registrable_sld(host)

    # OFFICIAL: a NON-GENERIC host label is named in the query. This lifts the
    # vendor's OWN domain when the query mentions the vendor — e.g. query
    # "…AWS CloudFront…" → host labels {cloudfront, amazonaws} or {aws, amazon}
    # intersect query tokens. Generic labels (com/amazonaws/github/…) are
    # excluded so a hosting/CDN/registrar domain isn't mistaken for the brand.
    q_tokens = brand_tokens(query)
    if host_brand_labels(url) & q_tokens:
        return "official"

    # PRIMARY: structurally-authoritative host, query-independent.
    if any(host == s.lstrip(".") or host.endswith(s) for s in _PRIMARY_HOST_SUFFIXES):
        return "primary"
    if any(host.startswith(p) for p in _PRIMARY_SUBDOMAIN_PREFIXES):
        return "primary"

    # AGGREGATOR: SEO/price-farm tells in the SLD.
    if _AGGREGATOR_SLD_RE.search(sld):
        return "aggregator"

    return "neutral"


def authority_label(url: str, query: str = "") -> str:
    """Human-readable authority tag for format.py (empty string if neutral)."""
    return _LABEL[classify_authority(url, query)]

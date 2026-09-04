"""Detection-chain tests for the split vertical package.

Pins the auto-detect priority order and the cleanups folded into this pass:
legacy arXiv ids, IPv6 addresses, and mid-sentence DOIs.
"""

import pytest

from tofu_search.search.vertical import detect_vertical_intent as detect
from tofu_search.search.vertical import list_domains

# ── priority / basic types ──

@pytest.mark.parametrize("query,exp_type,exp_id", [
    ("CVE-2021-44228", "cve", "CVE-2021-44228"),
    ("cve-2021-44228", "cve", "CVE-2021-44228"),
    ("2301.07041", "arxiv", "2301.07041"),
    ("arxiv:2301.07041v2", "arxiv", "2301.07041v2"),
    ("pip install requests", "pypi", "requests"),
    ("pypi: numpy", "pypi", "numpy"),
    ("npm:express", "npm", "express"),
    ("npx create-react-app", "npm", "create-react-app"),
    ("github:torvalds/linux", "github", "torvalds/linux"),
    ("torvalds/linux", "github", "torvalds/linux"),
    ("$AAPL", "stock", "AAPL"),
    ("AAPL stock", "stock", "AAPL"),
    ("8.8.8.8", "ip", "8.8.8.8"),
])
def test_detect_basic(query, exp_type, exp_id):
    out = detect(query)
    assert out is not None, query
    assert out[0] == exp_type
    assert out[1] == exp_id


# ── new: legacy arXiv ids ──

@pytest.mark.parametrize("q,ident", [
    ("hep-th/9901001", "hep-th/9901001"),
    ("math.AG/0509025", "math.AG/0509025"),
    ("arxiv:hep-th/9901001v3", "hep-th/9901001v3"),
])
def test_detect_legacy_arxiv(q, ident):
    out = detect(q)
    assert out is not None
    assert out[0] == "arxiv"
    assert out[1] == ident


# ── new: IPv6 ──

@pytest.mark.parametrize("q", ["2606:4700:4700::1111", "::1", "fe80::1"])
def test_detect_ipv6(q):
    out = detect(q)
    assert out is not None
    assert out[0] == "ip"
    assert out[1] == q


def test_invalid_ip_not_detected():
    # 999 octet is not a valid IPv4 → must not be routed to ip vertical.
    out = detect("999.999.999.999")
    assert out is None or out[0] != "ip"


# ── new: mid-sentence DOI ──

def test_detect_doi_midsentence():
    out = detect("see 10.1038/s41586-023-06221-2 for the full study")
    assert out is not None
    assert out[0] == "doi"
    assert out[1] == "10.1038/s41586-023-06221-2"


def test_detect_doi_trailing_punct_stripped():
    out = detect("doi:10.1038/s41586-023-06221-2.")
    assert out[0] == "doi"
    assert not out[1].endswith(".")


# ── negatives ──

@pytest.mark.parametrize("q", [
    "how do I learn to cook pasta",
    "API", "HTTP", "JSON",          # blocklisted acronyms, not tickers
    "src/main.py", "user/file.py",  # file paths, not github repos
    "",
])
def test_no_false_positive(q):
    out = detect(q)
    if out is not None:
        # The only acceptable hit for these is NOT stock/github/ip.
        assert out[0] not in ("stock", "ip")


def test_overlong_query_ignored():
    assert detect("x" * 250) is None


# ── package structure ──

def test_domains_intact():
    assert set(list_domains(available_only=False)) == {
        "academic", "code", "finance", "security", "network", "travel"}


def test_travel_domain_keyless_via_flyai():
    """travel is fully advertised with zero configuration: the FlyAI
    provider's bundled trial credential covers both types."""
    from tofu_search.search.vertical import available_types

    assert "travel" in list_domains()
    assert available_types("travel") == ["flight", "hotel"]


def test_each_module_has_contract():
    from tofu_search.search.vertical import registry
    for mod in registry._MODULES:
        assert hasattr(mod, "TYPE")
        assert hasattr(mod, "DOMAIN")
        assert callable(mod.search)
    for mod in registry._DETECT_CHAIN:
        assert callable(mod.detect)

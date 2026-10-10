"""Baseline safety-net tests for the pure (network-free) helpers.

These pin current behaviour BEFORE the search/fetch logic is refactored, so a
regression in the upcoming changes is caught immediately.
"""

from tofu_search import configure, get_config
from tofu_search.fetch.core import extract_urls_from_text
from tofu_search.search.dedup import dedup_by_content
from tofu_search.search.rerank import rerank_by_bm25
from tofu_search.search.vertical import detect_vertical_intent

# ── config ──

def test_configure_roundtrip_and_isolation():
    configure(fetch_top_n=11, filter_min_chars=1234)
    cfg = get_config()
    assert cfg.fetch_top_n == 11
    assert cfg.filter_min_chars == 1234


def test_config_copy_does_not_mutate_global():
    base = get_config()
    derived = base.copy(fetch_top_n=99)
    assert derived.fetch_top_n == 99
    assert get_config().fetch_top_n != 99  # global untouched


def test_has_llm():
    assert get_config().has_llm() is False
    configure(llm_api_key="sk-test")
    assert get_config().has_llm() is True


# ── dedup ──

def test_dedup_removes_near_duplicates():
    results = [
        {"title": "Python asyncio tutorial", "snippet": "Learn async await in Python", "url": "https://a.com"},
        {"title": "Python asyncio tutorial", "snippet": "Learn async await in Python today", "url": "https://b.com"},
        {"title": "Completely different topic", "snippet": "Rust ownership and borrowing", "url": "https://c.com"},
    ]
    out = dedup_by_content(results)
    urls = {r["url"] for r in out}
    assert "https://a.com" in urls  # first copy kept
    assert "https://c.com" in urls  # distinct kept
    assert len(out) == 2


def test_dedup_keeps_distinct_results():
    results = [
        {"title": "Apples", "snippet": "fruit", "url": "https://a.com"},
        {"title": "Databases", "snippet": "postgres mysql", "url": "https://b.com"},
    ]
    assert len(dedup_by_content(results)) == 2


def test_dedup_trivial_sizes():
    assert dedup_by_content([]) == []
    one = [{"title": "x", "snippet": "y", "url": "https://a.com"}]
    assert dedup_by_content(one) == one


# ── rerank ──

def test_rerank_orders_by_relevance():
    results = [
        {"title": "Cooking pasta", "snippet": "boil water", "url": "https://a.com"},
        {"title": "Python asyncio guide", "snippet": "asyncio event loop coroutine await", "url": "https://b.com"},
        {"title": "Gardening", "snippet": "plant tomatoes", "url": "https://c.com"},
    ]
    out = rerank_by_bm25("python asyncio", results, top_k=2)
    assert len(out) == 2
    assert out[0]["url"] == "https://b.com"  # most relevant first


def test_rerank_short_list_returned_as_is():
    results = [{"title": "x", "snippet": "y", "url": "https://a.com"}]
    assert rerank_by_bm25("anything", results, top_k=5) == results


def test_rerank_empty():
    assert rerank_by_bm25("q", [], top_k=5) == []


# ── vertical intent detection ──

def test_detect_cve():
    out = detect_vertical_intent("CVE-2021-44228")
    assert out is not None
    domain, identifier, _ = out
    assert domain == "cve"
    assert identifier.upper() == "CVE-2021-44228"


def test_detect_arxiv():
    out = detect_vertical_intent("2301.07041")
    assert out is not None
    assert out[0] == "arxiv"


def test_ticker_blocklist_blocks_common_words():
    # Common acronyms must NOT be routed to the stock vertical.
    for word in ("API", "HTTP", "JSON", "THE", "AND"):
        out = detect_vertical_intent(word)
        if out is not None:
            assert out[0] != "stock", f"{word} wrongly detected as stock ticker"


def test_no_intent_for_plain_prose():
    assert detect_vertical_intent("how do I learn to cook pasta") is None


# ── URL extraction ──

def test_extract_urls_basic():
    text = "see https://example.com/page and http://foo.org/bar for details"
    urls = extract_urls_from_text(text)
    assert "https://example.com/page" in urls
    assert any("foo.org" in u for u in urls)


def test_extract_urls_empty():
    assert extract_urls_from_text("") == []
    assert extract_urls_from_text("no links here") == []

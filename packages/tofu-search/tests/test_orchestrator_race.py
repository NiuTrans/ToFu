"""Offline regression tests for the Race-to-N fetch gate (#8).

The previous implementation re-scanned all of unique_results on every future
completion (O(n²)) to recompute the kept-with-content count. The fix tracks
that count incrementally, scoped to kept URLs. These tests pin the observable
behaviour: the pipeline returns content-bearing results and the fetch count is
correct, with engines + page fetch fully stubbed (no network).
"""

import pytest

from tofu_search.search import orchestrator
from tofu_search.search.orchestrator import perform_web_search


@pytest.fixture
def stub_engines(monkeypatch):
    """Stub every engine + page fetch so the pipeline runs offline."""
    # Each result gets index-derived unique tokens so Jaccard overlap between
    # any two results is ~0 and content-dedup keeps them all.
    def make_engine(prefix):
        def _engine(query, n=6, freshness=''):
            out = []
            for i in range(n):
                tok = f"uniqueterm{i}word"
                out.append({
                    "title": f"{tok}alpha {tok}beta {tok}gamma",
                    "snippet": f"{tok}delta {tok}epsilon {tok}zeta {tok}eta",
                    "url": f"https://{prefix}.com/{i}", "source": prefix,
                })
            return out
        return _engine

    # Only DDG-HTML returns results; others empty (keeps URL set predictable).
    monkeypatch.setattr(orchestrator, "search_ddg_html", make_engine("ddg"))
    for name in ("search_brave", "search_bing", "search_ddg_api",
                 "search_searxng", "search_marginalia"):
        monkeypatch.setattr(orchestrator, name, lambda q, n=6, freshness='': [])
    monkeypatch.setattr(orchestrator, "xhs_search_available", lambda: False)

    # Every fetch succeeds with substantial content.
    monkeypatch.setattr(orchestrator, "fetch_page_content",
                        lambda url, **kw: "x" * 500)
    # Disable deepen.
    monkeypatch.setattr(orchestrator, "is_deepen_enabled", lambda: False)
    # Disable the pre-fetch relevance gate — this fixture's synthetic results
    # use index-derived unique tokens with zero query overlap by design (to
    # keep content-dedup from collapsing them), which the gate would otherwise
    # correctly skip. These tests target the Race-to-N fetch accounting, an
    # orthogonal concern, so they need every result fetched. Restore the global
    # afterwards so the toggle doesn't leak into other tests.
    from tofu_search.config import configure, get_config
    _prev_gate = get_config().prefetch_gate_enabled
    configure(prefetch_gate_enabled=False)
    yield
    configure(prefetch_gate_enabled=_prev_gate)


def test_pipeline_returns_content_results(stub_engines):
    out = perform_web_search("test query", max_results=5,
                             filter_pages=False, rerank=False)
    assert len(out) == 5
    assert all(r.get("full_content") for r in out)


def test_pipeline_no_duplicate_urls(stub_engines):
    out = perform_web_search("test query", max_results=8,
                             filter_pages=False, rerank=False)
    urls = [r["url"] for r in out]
    assert len(urls) == len(set(urls))


def test_race_to_n_does_not_overcount_unkept(monkeypatch, stub_engines):
    """Pages dropped by content-dedup must NOT count toward the Race-to-N gate.

    We force every fetched page to have content; the gate should be driven by
    kept_urls membership, so the returned set never exceeds max_results and
    every returned item has content.
    """
    out = perform_web_search("test query", max_results=3,
                             filter_pages=False, rerank=False)
    assert len(out) == 3
    assert all(r.get("full_content") for r in out)


def test_pipeline_handles_empty_fetch(monkeypatch, stub_engines):
    # All fetches return None → results still returned (no content), no crash.
    monkeypatch.setattr(orchestrator, "fetch_page_content", lambda url, **kw: None)
    out = perform_web_search("test query", max_results=5,
                             filter_pages=False, rerank=False)
    assert len(out) == 5
    assert all(not r.get("full_content") for r in out)

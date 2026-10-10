"""Wall-clock deadline regression tests (the "wedged search" incident).

Two root-cause fixes are covered:

1. ``perform_web_search`` gained a HARD wall-clock deadline
   (``config.search_deadline_secs``). Before it, the ONLY caps were a 20s
   engine ``as_completed`` and a 90s fetch ``as_completed`` — and the 90s only
   short-circuited once ``kept_ok >= target_ok``, a count a niche-domain query
   (mostly dead/paywalled hosts) never reaches. So the call hung the full 90s
   (plus the LLM-filter/deepen/rerank tail). The deadline force-returns whatever
   was gathered, tagged ``_deadline_hit``, INDEPENDENT of ``target_ok``.

2. ``fetch_page_content`` gained a per-URL total-time cap
   (``config.fetch_url_deadline_secs``) that bounds the WHOLE fallback chain
   (HTTP body-download + browser + Playwright) so one dead host can't stack
   per-hop timeouts into 60s+.

Both are exercised against SIMULATED slow/dead hosts (a fetch stub that sleeps
far past the budget) with NO network. Each test carries a NEUTER-BITE sibling:
disable the knob (set the deadline to 0 = legacy unbounded) and assert the call
now EXCEEDS the budget — proving the deadline, not luck, is what bounds it.

Run: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest tests/test_deadline.py -v
"""

import time

import pytest

from tofu_search.config import configure, get_config
from tofu_search.search import orchestrator
from tofu_search.search.orchestrator import perform_web_search

# ── One dead-host cluster: engines return fast, every fetch sleeps forever ──
_SLOW_FETCH_SECS = 30      # each URL "hangs" this long (>> any test deadline)
_TEST_DEADLINE = 4         # wall-clock budget under test (seconds)


@pytest.fixture
def slow_hosts(monkeypatch):
    """Engines return instantly; every page fetch sleeps past the deadline.

    This is the niche-academic-query shape: SERPs come back, but the hosts are
    unreachable/slow, so Race-to-N never reaches target_ok and the pipeline is
    at the mercy of the fetch-wait ceiling.
    """
    def _engine(query, n=6, freshness=''):
        out = []
        for i in range(n):
            tok = f"deadterm{i}word"
            out.append({
                "title": f"{tok}alpha {tok}beta {tok}gamma",
                "snippet": f"{tok}delta {tok}epsilon {tok}zeta",
                "url": f"https://dead-host-{i}.example/{i}", "source": "DDG-HTML",
            })
        return out

    monkeypatch.setattr(orchestrator, "search_ddg_html", _engine)
    for name in ("search_brave", "search_bing", "search_ddg_api",
                 "search_searxng", "search_marginalia"):
        monkeypatch.setattr(orchestrator, name, lambda q, n=6, freshness='': [])
    monkeypatch.setattr(orchestrator, "xhs_search_available", lambda: False)
    # Browser fallback (fires when 0 results) must also not hang the test.
    monkeypatch.setattr(orchestrator, "search_via_browser", lambda q, n: [])
    monkeypatch.setattr(orchestrator, "is_deepen_enabled", lambda: False)

    def _slow_fetch(url, **kw):
        time.sleep(_SLOW_FETCH_SECS)
        return "x" * 500

    monkeypatch.setattr(orchestrator, "fetch_page_content", _slow_fetch)

    # These synthetic results share no query terms → the prefetch gate would
    # (correctly) skip them; disable it so they ARE submitted to the slow
    # fetch pool, which is the exact wedge we're bounding.
    _prev_gate = get_config().prefetch_gate_enabled
    _prev_deadline = get_config().search_deadline_secs
    configure(prefetch_gate_enabled=False)
    yield
    configure(prefetch_gate_enabled=_prev_gate, search_deadline_secs=_prev_deadline)


# ═══════════════════════════════════════════════════════════════════
#  Item 1 — perform_web_search wall-clock deadline
# ═══════════════════════════════════════════════════════════════════

def test_deadline_forces_partial_return(slow_hosts):
    """With the deadline set, the call returns within ~deadline + slack."""
    configure(search_deadline_secs=_TEST_DEADLINE)
    t0 = time.time()
    out = perform_web_search("niche academic query", max_results=6,
                             filter_pages=False, rerank=False)
    elapsed = time.time() - t0

    # Bounded: budget + generous slack for one in-flight fetch hop + teardown.
    # WITHOUT the deadline this would take ~min(90s, _SLOW_FETCH_SECS)=30s.
    assert elapsed < _TEST_DEADLINE + 8, (
        f"perform_web_search took {elapsed:.1f}s — deadline ({_TEST_DEADLINE}s) "
        f"did not bound it")
    # The diag marker must record the deadline firing.
    assert getattr(out, "_deadline_hit", False) is True, (
        "expected _deadline_hit=True on a budget-blown call")


def test_deadline_disabled_hangs_past_budget(slow_hosts):
    """NEUTER-BITE: deadline=0 (legacy) → the call is NOT bounded by the budget.

    This proves the deadline itself — not incidental fast teardown — is what
    bounds the call. With the cap off, the fetch-wait loop blocks on the slow
    hosts well past _TEST_DEADLINE (up to the legacy 90s ceiling / fetch sleep).
    """
    configure(search_deadline_secs=0)
    t0 = time.time()
    out = perform_web_search("niche academic query", max_results=6,
                             filter_pages=False, rerank=False)
    elapsed = time.time() - t0

    assert elapsed >= _TEST_DEADLINE + 8, (
        f"NEUTER FAILED: with deadline disabled the call returned in "
        f"{elapsed:.1f}s — it should have hung on the slow hosts well past "
        f"the {_TEST_DEADLINE}s budget (proving the deadline is load-bearing)")
    assert getattr(out, "_deadline_hit", False) is False


# ═══════════════════════════════════════════════════════════════════
#  Item 2 — per-URL fetch total-time cap
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def slow_url_transport(monkeypatch):
    """Simulate a dead host whose fallback chain hangs on every hop.

    Primary HTTP raises a Timeout, then the browser fallback sleeps 20s, then
    Playwright sleeps 20s — the exact per-hop stacking a single URL could burn
    without an outer cap. We patch the module-level fallback callables that
    ``fetch_page_content`` binds its deadline-aware wrappers around.
    """
    import requests

    from tofu_search.fetch import core

    # A real dead host CONSUMES its read timeout before failing — that's what
    # burns the per-URL budget. Simulate 5s of dead-connect, then raise, so by
    # the time the browser fallback would fire the budget is already blown and
    # the deadline-aware wrapper SKIPS it.
    def _boom_http(url, timeout, verify=True, legacy_ssl=False, deadline_ts=None):
        time.sleep(5)
        raise requests.exceptions.Timeout("simulated dead host")

    def _slow_browser(url, max_chars, reason='unknown'):
        time.sleep(20)
        return "browser text " * 20

    def _slow_playwright(url, max_chars, timeout):
        time.sleep(20)
        return "pw text " * 20

    monkeypatch.setattr(core, "_do_request", _boom_http)
    monkeypatch.setattr(core, "_try_browser_fetch", _slow_browser)
    monkeypatch.setattr(core, "_try_playwright_fallback", _slow_playwright)
    # Neutralise readers / auth / circuit / skip gates so we reach the HTTP path.
    monkeypatch.setattr(core, "_get_reader", lambda url: None)
    monkeypatch.setattr(core, "get_auth_source_provider", lambda: None)
    monkeypatch.setattr(core, "_should_fetch", lambda url: True)
    monkeypatch.setattr(core, "_is_known_spa", lambda url: False)
    _prev = get_config().fetch_url_deadline_secs
    yield core
    configure(fetch_url_deadline_secs=_prev)


def test_per_url_cap_bounds_fallback_chain(slow_url_transport):
    """With the per-URL cap, one dead host's whole chain is bounded."""
    core = slow_url_transport
    configure(fetch_url_deadline_secs=3)
    t0 = time.time()
    result = core.fetch_page_content("https://dead.example/paper")
    elapsed = time.time() - t0
    # HTTP raises instantly → budget blown before the browser hop (20s) →
    # browser + Playwright are SKIPPED. Bounded well under one 20s hop.
    assert elapsed < 10, (
        f"fetch_page_content took {elapsed:.1f}s — per-URL cap (3s) did not "
        f"skip the slow fallback hops")
    assert result is None


def test_per_url_cap_disabled_stacks_hops(slow_url_transport):
    """NEUTER-BITE: cap=0 → the browser+Playwright hops both run and stack.

    Proves the per-URL deadline is what skips the hops: with it off, the dead
    host chains the 20s browser fallback (returns text → whole chain ~20s),
    which is far past the 3s budget the capped test enforced.
    """
    core = slow_url_transport
    configure(fetch_url_deadline_secs=0)
    t0 = time.time()
    core.fetch_page_content("https://dead.example/paper")
    elapsed = time.time() - t0
    assert elapsed >= 15, (
        f"NEUTER FAILED: with the per-URL cap disabled the fetch returned in "
        f"{elapsed:.1f}s — the 20s browser fallback hop should have run "
        f"unbounded (proving the cap is load-bearing)")

"""tests/test_xhs_guard.py — XHS engine account-risk guard.

The XHS engine fires on EVERY web search while the source is connected, so
without pacing the user's chat frequency becomes the request rate against
their logged-in session — the exact pattern XHS account risk-control flags.
The guard applies the three read-only-search consensus mitigations:

  * pacing + jitter between real page loads (with skip-over-budget),
  * a same-keyword TTL cache,
  * consecutive-empty backoff cooldown (a captcha / login-redirect wall
    scrapes as zero note cards; several in a row = stop touching the site).

All offline: the Playwright pool method is monkeypatched; the auth-source
provider seam is a stub; config knobs go through configure() (conftest
restores the global config around every test).

Run: python -m pytest tests/test_xhs_guard.py -q
"""

import time

import pytest

import tofu_search
from tofu_search.search.engines import xhs


class _FakeAuthProvider:
    """Always-connected xiaohongshu.com source."""

    def match_source(self, url):
        return None

    def get_source(self, domain):
        if domain != 'xiaohongshu.com':
            return None
        return {'domain': 'xiaohongshu.com', 'enabled': True,
                'cookies': [{'name': 'web_session', 'value': 'tok'}], 'proxy': ''}


@pytest.fixture(autouse=True)
def _guard_fresh(monkeypatch):
    """Reset guard state, register the connected source, and install a pool
    spy whose call count each test asserts on."""
    import tofu_search.fetch.playwright_pool as pp

    xhs._GUARD.reset()
    tofu_search.register_auth_source_provider(_FakeAuthProvider())
    calls = []

    def fake_search(url, cookies, proxy='', timeout=20, extractor_js='', wait_selector=''):
        calls.append(url)
        return fake_search.items

    fake_search.items = []
    monkeypatch.setattr(pp._pw_pool, 'search_authenticated', fake_search)
    yield calls
    tofu_search.register_auth_source_provider(None)


def _items(n=2):
    return [{'title': f'note {i}', 'snippet': 's',
             'url': f'https://www.xiaohongshu.com/explore/{i}'} for i in range(n)]


@pytest.mark.unit
class TestPacing:
    def test_min_interval_spaces_real_loads(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()
        tofu_search.configure(xhs_min_interval_s=0.4, xhs_cache_ttl_s=0)

        xhs.search_xhs('query one')
        t0 = time.time()
        xhs.search_xhs('query two')   # different keyword → no cache, must pace
        elapsed = time.time() - t0

        assert len(_guard_fresh) == 2
        assert elapsed >= 0.4  # the second load waited out the interval

    def test_over_budget_wait_skips_instead_of_stalling(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()
        tofu_search.configure(xhs_min_interval_s=0.1, xhs_cache_ttl_s=0)

        xhs.search_xhs('first')
        # A huge interval makes the next slot land beyond the latency budget.
        tofu_search.configure(xhs_min_interval_s=100.0)
        t0 = time.time()
        assert xhs.search_xhs('second') == []
        assert time.time() - t0 < xhs._MAX_THROTTLE_WAIT_S
        assert len(_guard_fresh) == 1  # no second page load happened

    def test_zero_interval_disables_pacing(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0)
        xhs.search_xhs('a')
        xhs.search_xhs('b')
        assert len(_guard_fresh) == 2


@pytest.mark.unit
class TestQueryCache:
    def test_same_keyword_served_from_cache(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=600)

        first = xhs.search_xhs('ramen')
        second = xhs.search_xhs('  RAMEN ')  # normalized: same cache key

        assert len(_guard_fresh) == 1  # ONE real page load for two asks
        assert second == first
        # Cache returns defensive copies — mutating a hit must not rot it.
        second[0]['title'] = 'MUTATED'
        assert xhs.search_xhs('ramen')[0]['title'] != 'MUTATED'
        assert len(_guard_fresh) == 1

    def test_zero_ttl_disables_cache(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0)
        xhs.search_xhs('ramen')
        xhs.search_xhs('ramen')
        assert len(_guard_fresh) == 2

    def test_empty_results_are_never_cached(self, _guard_fresh):
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=600,
                              xhs_backoff_cooldown_s=0)
        xhs.search_xhs('niche')
        xhs.search_xhs('niche')
        assert len(_guard_fresh) == 2  # each ask retried — empties not cached


@pytest.mark.unit
class TestBackoff:
    def test_consecutive_empties_trip_cooldown(self, _guard_fresh):
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                              xhs_backoff_cooldown_s=1800)
        for q in ('q1', 'q2', 'q3'):
            assert xhs.search_xhs(q) == []
        assert len(_guard_fresh) == xhs._BACKOFF_AFTER_EMPTY

        # Cooldown: further queries are refused WITHOUT touching the site.
        assert xhs.search_xhs('q4') == []
        assert len(_guard_fresh) == xhs._BACKOFF_AFTER_EMPTY
        assert xhs._GUARD.in_cooldown() is True

    def test_non_empty_result_resets_the_counter(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                              xhs_backoff_cooldown_s=1800)
        xhs.search_xhs('e1')
        xhs.search_xhs('e2')                      # 2 consecutive empties
        pp._pw_pool.search_authenticated.items = _items()
        xhs.search_xhs('hit')                     # resets the streak
        pp._pw_pool.search_authenticated.items = []
        xhs.search_xhs('e3')
        xhs.search_xhs('e4')                      # only 2 again — no cooldown
        assert len(_guard_fresh) == 5
        assert xhs._GUARD.in_cooldown() is False

    def test_hard_error_counts_as_empty(self, _guard_fresh, monkeypatch):
        """None from the pool (timeout / crash) feeds the same backoff — a
        wedged transport is also a reason to stop hammering."""
        import tofu_search.fetch.playwright_pool as pp
        monkeypatch.setattr(pp._pw_pool, 'search_authenticated',
                            lambda *a, **k: None)
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                              xhs_backoff_cooldown_s=1800)
        for q in ('x1', 'x2', 'x3'):
            xhs.search_xhs(q)
        assert xhs._GUARD.in_cooldown() is True

    def test_cooldown_expires(self, _guard_fresh):
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()
        tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                              xhs_backoff_cooldown_s=1800)
        # Trip the cooldown, then pretend it elapsed.
        for q in ('q1', 'q2', 'q3'):
            xhs.search_xhs(q)
        xhs._GUARD._cooldown_until = time.time() - 1
        out = xhs.search_xhs('recovered')
        assert len(out) == 2
        assert len(_guard_fresh) == 4

"""tests/test_xhs_browser_reroute.py — XHS browser-first reroute (tofu-search 0.7.0).

Contract (docs/SITE_KNOWLEDGE_LAYER_DESIGN.md in the chatui repo): the XHS
engine must prefer the host browser (the user's REAL Chrome — native login,
same IP/fingerprint as the site's trust decisions) over the server-side
Playwright pool replaying exported cookies (headless fingerprint + foreign
IP = the classic account risk-control trigger that got the owner's account
warned).

  * browser connected → ``provider.scrape`` is used, the pool is NOT touched;
  * ``scrape`` returning None (path unavailable / crashed) → pool fallback;
  * ``scrape`` returning [] (path worked, genuinely zero cards) → NO pool
    fallback — the empty feeds the risk-control backoff instead;
  * availability: enabled + (cookies OR live browser) — a connected browser
    alone (no stored cookies) is enough, since the browser IS logged in;
  * the account-risk guard (pacing / cache / empty-backoff) applies to BOTH
    paths — it protects the account, not a transport.

All offline: the pool method and both provider seams are fakes.

Run: python -m pytest tests/test_xhs_browser_reroute.py -q
"""

import pytest

import tofu_search
from tofu_search.search.engines import xhs


def _cookies():
    return [{'name': 'web_session', 'value': 'tok'}]


class _FakeAuthProvider:
    def __init__(self, cookies):
        self._cookies = cookies

    def match_source(self, url):
        return None

    def get_source(self, domain):
        if domain != 'xiaohongshu.com':
            return None
        return {'domain': 'xiaohongshu.com', 'enabled': True,
                'cookies': self._cookies, 'proxy': ''}


class _FakeBrowserProvider(tofu_search.BrowserProvider):
    """Records scrape() calls; result programmable; can simulate a crash."""

    def __init__(self, result=None, connected=True, raises=False):
        self._result = result
        self._connected = connected
        self._raises = raises
        self.calls = []

    def is_connected(self):
        return self._connected

    def scrape(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self._raises:
            raise RuntimeError('extension gone mid-command')
        return self._result


def _items(n=2):
    return [{'title': f'note {i}', 'snippet': 's',
             'url': f'https://www.xiaohongshu.com/explore/{i}?xsec_token=t{i}'}
            for i in range(n)]


@pytest.fixture
def rig(monkeypatch):
    """Fresh guard + pool spy; each test installs its own providers."""
    import tofu_search.fetch.playwright_pool as pp

    xhs._GUARD.reset()
    pool_calls = []

    def fake_pool(url, cookies, proxy='', timeout=20, extractor_js='', wait_selector=''):
        pool_calls.append(url)
        return fake_pool.items

    fake_pool.items = []
    monkeypatch.setattr(pp._pw_pool, 'search_authenticated', fake_pool)
    tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                          xhs_backoff_cooldown_s=0)
    yield pool_calls
    tofu_search.register_auth_source_provider(None)
    tofu_search.register_browser_provider(None)


@pytest.mark.unit
class TestBrowserPreferred:
    def test_scrape_used_and_pool_untouched(self, rig):
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        browser = _FakeBrowserProvider(result=_items())
        tofu_search.register_browser_provider(browser)

        out = xhs.search_xhs('咖啡')

        assert [r['title'] for r in out] == ['note 0', 'note 1']
        assert len(browser.calls) == 1, 'the live browser must be the primary path'
        assert rig == [], 'pool replay must NOT fire while the browser delivers'
        # The extractor + wait selector ride along to the host.
        kw = browser.calls[0][1]
        assert kw.get('extractor_js'), 'the card extractor JS must reach the browser'
        assert kw.get('wait_selector'), 'the note-card wait selector must reach the browser'

    def test_scrape_none_falls_back_to_pool(self, rig):
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        tofu_search.register_browser_provider(_FakeBrowserProvider(result=None))
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()

        out = xhs.search_xhs('咖啡')

        assert len(out) == 2, 'pool fallback must deliver when the browser path is unavailable'
        assert len(rig) == 1

    def test_scrape_crash_falls_back_to_pool(self, rig):
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        tofu_search.register_browser_provider(_FakeBrowserProvider(raises=True))
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()

        out = xhs.search_xhs('咖啡')

        assert len(out) == 2
        assert len(rig) == 1

    def test_scrape_empty_is_a_real_empty_not_a_fallback(self, rig):
        """[] means 'browser worked, zero cards' — falling back to the pool
        would double-hit the account for the same query."""
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        tofu_search.register_browser_provider(_FakeBrowserProvider(result=[]))

        out = xhs.search_xhs('niche query')

        assert out == []
        assert rig == [], 'genuine empty from the browser must NOT re-hit via the pool'


@pytest.mark.unit
class TestAvailability:
    def test_browser_alone_makes_engine_available(self, rig):
        """No stored cookies, but the live browser is logged in → searchable."""
        tofu_search.register_auth_source_provider(_FakeAuthProvider(cookies=[]))
        browser = _FakeBrowserProvider(result=_items())
        tofu_search.register_browser_provider(browser)

        assert xhs.xhs_search_available() is True
        out = xhs.search_xhs('咖啡')
        assert len(out) == 2
        assert rig == [], 'no cookies → pool path impossible, browser must carry it'

    def test_neither_cookies_nor_browser_is_unavailable(self, rig):
        tofu_search.register_auth_source_provider(_FakeAuthProvider(cookies=[]))
        tofu_search.register_browser_provider(None)

        assert xhs.xhs_search_available() is False
        assert xhs.search_xhs('咖啡') == []
        assert rig == []

    def test_cookies_alone_keep_pool_path(self, rig):
        """Browser offline → the historical pool replay still works."""
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        tofu_search.register_browser_provider(None)
        import tofu_search.fetch.playwright_pool as pp
        pp._pw_pool.search_authenticated.items = _items()

        assert xhs.xhs_search_available() is True
        assert len(xhs.search_xhs('咖啡')) == 2
        assert len(rig) == 1


@pytest.mark.unit
class TestGuardAppliesToBothPaths:
    def test_browser_results_feed_the_query_cache(self, rig):
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        browser = _FakeBrowserProvider(result=_items())
        tofu_search.register_browser_provider(browser)
        tofu_search.configure(xhs_cache_ttl_s=600)

        first = xhs.search_xhs('ramen')
        second = xhs.search_xhs('  RAMEN ')

        assert second == first
        assert len(browser.calls) == 1, 'cache must shield the account on BOTH paths'

    def test_browser_empties_trip_the_backoff(self, rig):
        tofu_search.register_auth_source_provider(_FakeAuthProvider(_cookies()))
        tofu_search.register_browser_provider(_FakeBrowserProvider(result=[]))
        tofu_search.configure(xhs_backoff_cooldown_s=1800)

        for q in ('q1', 'q2', 'q3'):
            xhs.search_xhs(q)

        assert xhs._GUARD.in_cooldown() is True, (
            'zero-card scrapes via the browser are indistinguishable from a '
            'risk wall — the same backoff must trip')

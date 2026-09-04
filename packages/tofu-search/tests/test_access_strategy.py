"""tests/test_access_strategy.py — registry-driven access_strategy (P2).

The site registry row carries ``access_strategy`` (chatui lib/auth_sources,
merged into the FULL row tofu-search receives via AuthSourceProvider):

  * ``browser_first`` (default / absent) — live browser primary, pool replay
    fallback (P0 behaviour, unchanged);
  * ``cookies_replay`` — the OLD order: pool replay primary, browser
    fallback, for risk-tolerant sites or a usually-offline browser;
  * ``public`` — the site needs NO identity: engines/fetch skip the identity
    paths entirely and let the anonymous pipeline serve.

Also pins the last per-site hardcode removal in the login flow:
interactive_login's "login completed" cookie hints now come from the
registry row's ``fields`` spec, the module table only a standalone fallback.

All offline: providers are fakes registered through tofu_search.providers.
"""

import pytest

import tofu_search
from tofu_search.fetch import core as fetch_core
from tofu_search.fetch import interactive_login
from tofu_search.search.engines import xhs

pytestmark = pytest.mark.unit

CARDS = [{'title': 'note 0', 'url': 'https://www.xiaohongshu.com/explore/a'}]


def _source(strategy):
    row = {'domain': 'xiaohongshu.com', 'enabled': True,
           'cookies': [{'name': 'web_session', 'value': 'x',
                        'domain': '.xiaohongshu.com', 'path': '/'}]}
    if strategy is not None:
        row['access_strategy'] = strategy
    return row


class _Auth(tofu_search.AuthSourceProvider):
    def __init__(self, row):
        self._row = row

    def get_source(self, domain):
        return self._row

    def match_source(self, url):
        return self._row


class _Browser(tofu_search.BrowserProvider):
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def is_connected(self):
        return True

    def scrape(self, url, **kw):
        self.calls += 1
        return self.result


@pytest.fixture
def rig(monkeypatch):
    xhs._GUARD.reset()
    tofu_search.clear_site_drift_listeners()
    tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                          xhs_backoff_cooldown_s=0)
    yield
    tofu_search.register_browser_provider(None)
    tofu_search.register_auth_source_provider(None)
    tofu_search.clear_site_drift_listeners()
    xhs._GUARD.reset()


def _pool_spy(monkeypatch, result):
    import tofu_search.fetch.playwright_pool as pp
    spy = {'calls': 0}

    def fake(url, cookies, proxy='', timeout=20, extractor_js='',
             wait_selector=''):
        spy['calls'] += 1
        return result

    monkeypatch.setattr(pp._pw_pool, 'search_authenticated', fake)
    return spy


# ── xhs engine ordering ───────────────────────────────────

def test_public_strategy_disables_identity_paths(rig, monkeypatch):
    tofu_search.register_auth_source_provider(_Auth(_source('public')))
    browser = _Browser(CARDS)
    tofu_search.register_browser_provider(browser)
    pool = _pool_spy(monkeypatch, CARDS)
    assert xhs.search_xhs('咖啡') == []
    assert browser.calls == 0 and pool['calls'] == 0, (
        'public = no identity path may fire at all')


def test_cookies_replay_runs_pool_first(rig, monkeypatch):
    tofu_search.register_auth_source_provider(_Auth(_source('cookies_replay')))
    browser = _Browser(CARDS)
    tofu_search.register_browser_provider(browser)
    pool = _pool_spy(monkeypatch, CARDS)
    out = xhs.search_xhs('咖啡')
    assert len(out) == 1
    assert pool['calls'] == 1
    assert browser.calls == 0, (
        'replay-first: the browser must NOT fire while the pool delivers')


def test_cookies_replay_falls_back_to_browser(rig, monkeypatch):
    tofu_search.register_auth_source_provider(_Auth(_source('cookies_replay')))
    browser = _Browser(CARDS)
    tofu_search.register_browser_provider(browser)
    pool = _pool_spy(monkeypatch, None)   # pool unavailable → fallback
    out = xhs.search_xhs('咖啡')
    assert len(out) == 1
    assert pool['calls'] == 1 and browser.calls == 1


def test_browser_first_remains_the_default(rig, monkeypatch):
    """Absent access_strategy = browser first (P0 behaviour unchanged)."""
    tofu_search.register_auth_source_provider(_Auth(_source(None)))
    browser = _Browser(CARDS)
    tofu_search.register_browser_provider(browser)
    pool = _pool_spy(monkeypatch, CARDS)
    out = xhs.search_xhs('咖啡')
    assert len(out) == 1
    assert browser.calls == 1 and pool['calls'] == 0


# ── fetch core ordering ───────────────────────────────────

class _FetchRig:
    order = None
    browser_result = None
    replay_result = None


@pytest.fixture
def fetch_rig(monkeypatch):
    rig = _FetchRig()
    rig.order = []
    rig.browser_result = None
    rig.replay_result = None

    def fake_browser(url, max_chars, reason=''):
        rig.order.append(('browser', reason))
        return rig.browser_result

    def fake_replay(url, src, max_chars, timeout):
        rig.order.append(('replay', None))
        return rig.replay_result

    monkeypatch.setattr(fetch_core, '_try_browser_fetch', fake_browser)
    monkeypatch.setattr(fetch_core, '_try_authenticated_fetch', fake_replay)
    monkeypatch.setattr(fetch_core, '_should_fetch', lambda url, **kw: False)
    yield rig
    tofu_search.register_auth_source_provider(None)


def test_fetch_cookies_replay_order(rig, fetch_rig):
    tofu_search.register_auth_source_provider(_Auth(_source('cookies_replay')))
    fetch_rig.replay_result = 'article text'
    out = fetch_core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')
    assert out == 'article text'
    assert [leg for leg, _ in fetch_rig.order] == ['replay'], (
        'replay-first: browser must not fire while replay delivers')


def test_fetch_cookies_replay_browser_fallback(rig, fetch_rig):
    tofu_search.register_auth_source_provider(_Auth(_source('cookies_replay')))
    fetch_rig.replay_result = None            # replay unavailable
    fetch_rig.browser_result = 'browser text'
    out = fetch_core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')
    assert out == 'browser text'
    assert [leg for leg, _ in fetch_rig.order] == ['replay', 'browser']
    assert fetch_rig.order[1][1] == 'auth_source_browser_fallback'


def test_fetch_browser_first_order(rig, fetch_rig):
    tofu_search.register_auth_source_provider(_Auth(_source(None)))
    fetch_rig.browser_result = 'browser text'
    out = fetch_core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')
    assert out == 'browser text'
    assert [leg for leg, _ in fetch_rig.order] == ['browser']
    assert fetch_rig.order[0][1] == 'auth_source_browser_first'


def test_replay_never_fires_without_cookies(rig, fetch_rig):
    """A browser_first row matches with ZERO stored cookies (the live session
    is the credential). When the browser is unavailable, the replay leg must
    NO-OP — an anonymous pool load of a login wall is waste + bot traffic."""
    row = _source(None)
    row['cookies'] = []
    tofu_search.register_auth_source_provider(_Auth(row))
    fetch_rig.browser_result = None          # browser unavailable
    fetch_core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')
    assert [leg for leg, _ in fetch_rig.order] == ['browser'], (
        'no stored cookies = nothing to replay; the replay leg must not fire')


def test_fetch_public_skips_identity(rig, fetch_rig):
    tofu_search.register_auth_source_provider(_Auth(_source('public')))
    fetch_core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')
    assert fetch_rig.order == [], 'public = neither identity leg may fire'


# ── interactive login hints (registry-driven) ─────────────

def test_login_hints_from_registry_fields(rig):
    row = _source(None)
    row['fields'] = [{'name': 'sess_id'}, {'name': 'sess_token'}]
    tofu_search.register_auth_source_provider(_Auth(row))
    assert interactive_login._login_hints('xiaohongshu.com') == (
        'sess_id', 'sess_token')


def test_login_hints_fall_back_to_table(rig):
    """No provider / no fields → the standalone module table serves."""
    assert 'web_session' in interactive_login._login_hints('xiaohongshu.com')
    assert interactive_login._login_hints('unknown.example') == ()

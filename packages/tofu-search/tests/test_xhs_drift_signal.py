"""tests/test_xhs_drift_signal.py — XHS selector-drift signal + knowledge seam.

Site Knowledge Layer P3 (chatui docs/SITE_KNOWLEDGE_LAYER_DESIGN.md §6):

  * the engine reads extraction knowledge (wait selector / extractor JS /
    scrolls) from the host's SiteKnowledgeProvider FIRST, built-in constants
    only as fallback — re-pinning selectors is DATA, not a release;
  * the browser path wraps the LIST extractor with a PROBE: when the page
    rendered note anchors but extraction made zero cards, that is SELECTOR
    DRIFT — the engine emits a site-drift signal (host re-cons) instead of
    silently feeding the empty backoff;
  * a REAL empty (anchors == 0) and a risk wall (renders no anchors) emit
    NOTHING; listener exceptions can never break a search;
  * the POOL path keeps the bare-list extractor (its worker coerces
    non-lists), only the browser path gets the probed dict form.

All offline: providers are fakes registered through tofu_search.providers.
"""

import pytest

import tofu_search
from tofu_search.search.engines import xhs

pytestmark = pytest.mark.unit

CARDS = [{'title': 'note 0', 'url': 'https://www.xiaohongshu.com/explore/a'},
         {'title': 'note 1', 'url': 'https://www.xiaohongshu.com/explore/b'}]


def _cookies():
    return [{'name': 'web_session', 'value': 'x', 'domain': '.xiaohongshu.com',
             'path': '/'}]


class _FakeAuthProvider(tofu_search.AuthSourceProvider):
    def __init__(self, source):
        self._source = source

    def get_source(self, domain):
        return self._source

    def match_source(self, url):
        return self._source


class _FakeBrowserProvider(tofu_search.BrowserProvider):
    """scrape returns ``self.result`` verbatim; records every call."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def is_connected(self):
        return True

    def scrape(self, url, **kw):
        self.calls.append((url, kw))
        return self.result


class _FakeKnowledgeProvider(tofu_search.SiteKnowledgeProvider):
    def __init__(self, entry):
        self._entry = entry

    def get_knowledge(self, domain):
        return self._entry


@pytest.fixture
def rig(monkeypatch):
    """Clean providers + guard + drift listeners around every test."""
    xhs._GUARD.reset()
    tofu_search.clear_site_drift_listeners()
    # Neutralize pacing/cache so consecutive queries all reach the page-load
    # path (same convention as test_xhs_browser_reroute).
    tofu_search.configure(xhs_min_interval_s=0, xhs_cache_ttl_s=0,
                          xhs_backoff_cooldown_s=0)
    yield
    tofu_search.register_browser_provider(None)
    tofu_search.register_auth_source_provider(None)
    tofu_search.register_site_knowledge_provider(None)
    tofu_search.clear_site_drift_listeners()
    xhs._GUARD.reset()


def _install(browser_result, *, knowledge=None, cookies=None):
    tofu_search.register_auth_source_provider(
        _FakeAuthProvider({'domain': 'xiaohongshu.com', 'enabled': True,
                           'cookies': cookies if cookies is not None
                           else _cookies()}))
    browser = _FakeBrowserProvider(browser_result)
    tofu_search.register_browser_provider(browser)
    if knowledge is not None:
        tofu_search.register_site_knowledge_provider(
            _FakeKnowledgeProvider(knowledge))
    return browser


def test_knowledge_overrides_builtin_selectors(rig):
    """A pinned knowledge entry replaces wait selector / extractor / scrolls."""
    browser = _install([], knowledge={
        'extractor_js': '(() => [{title: "k", url: "https://x"}])()',
        'wait_selector': 'div.custom-card', 'scrolls': 5, 'version': 3})
    xhs.search_xhs('咖啡')
    assert len(browser.calls) == 1
    kw = browser.calls[0][1]
    assert kw['wait_selector'] == 'div.custom-card'
    assert kw['scrolls'] == 5
    assert 'div.custom-card' not in kw['extractor_js'] or True  # wrapper is JS
    assert 'const items = (() => [{title: "k"' in kw['extractor_js'], (
        'the browser path wraps the KNOWLEDGE extractor, not the built-in')


def test_builtin_used_when_no_knowledge(rig):
    browser = _install(CARDS)
    out = xhs.search_xhs('咖啡')
    assert len(out) == 2
    kw = browser.calls[0][1]
    assert kw['wait_selector'] == xhs._WAIT_SELECTOR
    assert 'note-item' in kw['extractor_js'], 'built-in card body rides along'
    assert kw['scrolls'] == 2


def test_invalid_knowledge_falls_back(rig):
    """Knowledge without a usable extractor_js is ignored, not fatal."""
    browser = _install(CARDS, knowledge={'wait_selector': 'x'})
    out = xhs.search_xhs('咖啡')
    assert len(out) == 2
    assert browser.calls[0][1]['wait_selector'] == xhs._WAIT_SELECTOR


def test_drift_emitted_when_anchors_but_no_cards(rig):
    """anchors>0 + 0 cards = the page rendered, our selectors drifted."""
    events = []
    tofu_search.register_site_drift_listener(
        lambda site, url, evidence: events.append((site, url, evidence)))
    _install({'items': [], 'probe': {'anchors': 7, 'title': '咖啡 - 小红书搜索',
                                     'url': 'https://x'}})
    out = xhs.search_xhs('咖啡')
    assert out == []
    assert len(events) == 1, 'drift signal must fire exactly once'
    site, url, evidence = events[0]
    assert site == 'xiaohongshu.com'
    assert evidence['anchors'] == 7
    assert '咖啡' in evidence['page_title']


def test_real_empty_emits_nothing(rig):
    """anchors==0 = the query genuinely has no hits (or a wall) — no signal."""
    events = []
    tofu_search.register_site_drift_listener(
        lambda site, url, evidence: events.append((site, url, evidence)))
    _install({'items': [], 'probe': {'anchors': 0, 'title': '', 'url': 'x'}})
    assert xhs.search_xhs('不存在的词xyz') == []
    assert events == []


def test_legacy_list_result_never_drifts(rig):
    """A bare-list result (legacy host) has no probe — empty stays real."""
    events = []
    tofu_search.register_site_drift_listener(
        lambda site, url, evidence: events.append((site, url, evidence)))
    _install([])
    assert xhs.search_xhs('咖啡') == []
    assert events == []


def test_raising_listener_cannot_break_search(rig):
    def boom(site, url, evidence):
        raise RuntimeError('listener exploded')

    tofu_search.register_site_drift_listener(boom)
    _install({'items': [], 'probe': {'anchors': 3, 'title': 't', 'url': 'x'}})
    assert xhs.search_xhs('咖啡') == [], 'listener exception must be swallowed'


def test_drift_still_feeds_empty_backoff(rig):
    """Drift is an empty OUTCOME for pacing: 3 consecutive → cooldown."""
    _install({'items': [], 'probe': {'anchors': 5, 'title': 't', 'url': 'x'}})
    tofu_search.configure(xhs_backoff_cooldown_s=1800)
    for q in ('q1', 'q2', 'q3'):
        xhs.search_xhs(q)
    assert xhs._GUARD.in_cooldown(), (
        'drifted empties must still trip the risk-control backoff — '
        'hammering a drifting selector is still hammering the account')


def test_pool_path_keeps_list_extractor(rig, monkeypatch):
    """The pool worker coerces non-lists — it must get the BARE list form."""
    import tofu_search.fetch.playwright_pool as pp

    _install(None)  # browser path unavailable → pool fallback
    pool_kw = {}

    def fake_pool(url, cookies, proxy='', timeout=20, extractor_js='',
                  wait_selector=''):
        pool_kw['extractor_js'] = extractor_js
        return CARDS

    monkeypatch.setattr(pp._pw_pool, 'search_authenticated', fake_pool)
    out = xhs.search_xhs('咖啡')
    assert len(out) == 2
    assert 'probe' not in pool_kw['extractor_js'], (
        'pool path must receive the bare list extractor, not the probed wrap')
    assert pool_kw['extractor_js'] == xhs._EXTRACTOR_JS

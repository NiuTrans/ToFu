"""Offline regression tests for the identity-fallback seams in fetch_page_content.

Contract (docs/FETCH_IDENTITY_PATHS_DESIGN.md in the chatui repo): when the
anonymous chain *succeeds* at the transport level but yields no content —
a 200 SPA shell, a bot/login wall (raw HTML or extracted text), a known-SPA
domain whose anonymous render came back empty, or an auth-source replay whose
stored cookies went stale — the pipeline must offer the URL to the host
browser provider (the user's live, logged-in browser) before giving up.

These tests drive the PUBLIC entry ``fetch_page_content`` offline (every
network/Playwright hop is monkeypatched) and assert the OUTCOME:
  * the browser fallback is invoked exactly once, with the right ``reason``;
  * its text, when present, is what the caller gets;
  * when the browser yields nothing, behaviour is byte-identical to the
    pre-seam pipeline (partial shell text / None / anonymous continuation);
  * when Playwright succeeds, the browser is NEVER consulted (over-trigger
    guard).

0.7.0 contract strengthening (SITE_KNOWLEDGE_LAYER_DESIGN.md): for an
auth-source-matched (login-walled) domain the browser is no longer the LAST
resort — it is tried FIRST (live session beats stored-cookie replay: native
signing, same IP/fingerprint; the server-side replay is the classic account
risk-control trigger). The replay becomes the fallback, and the old
``auth_source_failed`` post-replay escalation is subsumed by the single
``auth_source_browser_first`` call.

NEUTER anchors (deleting the seam line must turn the matching test red):
  * test_spa_shell_falls_to_browser            — the spa_shell seam
  * test_bot_wall_html_falls_to_browser        — the raw-HTML login_wall seam
  * test_bot_text_falls_to_browser             — the post-extraction login_wall seam
  * test_known_spa_falls_to_browser            — the known_spa seam
  * test_auth_source_browser_first_delivers    — the auth_source_browser_first seam

All offline: no network, no real browser, no real Playwright.
"""

import pytest

import tofu_search.fetch.core as core
from tofu_search.providers import (
    get_auth_source_provider,
    get_browser_provider,
    register_auth_source_provider,
    register_browser_provider,
)

_SHELL_HTML = b'<html><head><title>app</title></head><body><div id="app"></div></body></html>'
_BROWSER_TEXT = 'content only a logged-in browser can see — model list, prices, dates'


class _FakeResp:
    """Minimal stand-in for the requests.Response fetch_page_content consumes."""

    def __init__(self, content_type='text/html; charset=utf-8'):
        self.headers = {'Content-Type': content_type}
        self.encoding = 'utf-8'


@pytest.fixture(autouse=True)
def _restore_providers():
    saved_b = get_browser_provider()
    saved_a = get_auth_source_provider()
    try:
        yield
    finally:
        register_browser_provider(saved_b)
        register_auth_source_provider(saved_a)


@pytest.fixture
def harness(monkeypatch):
    """Common anonymous-chain fakes; individual tests override what they need."""
    calls = {'browser': [], 'do_request': 0, 'pw': 0}

    def fake_do_request(url, timeout, **kwargs):
        calls['do_request'] += 1
        return _FakeResp(), _SHELL_HTML

    def fake_browser(url, max_chars, reason='unknown'):
        calls['browser'].append(reason)
        return None  # default: browser has nothing; tests override

    def fake_pw(url, max_chars, timeout):
        calls['pw'] += 1
        return None  # default: anonymous render yields nothing

    monkeypatch.setattr(core, '_get_reader', lambda url: None)
    monkeypatch.setattr(core, '_should_fetch', lambda url: True)
    monkeypatch.setattr(core, '_is_known_spa', lambda url: False)
    monkeypatch.setattr(core, '_do_request', fake_do_request)
    monkeypatch.setattr(core, '_is_bot_protection', lambda html: False)
    monkeypatch.setattr(core, '_is_bot_extracted_text', lambda text: False)
    monkeypatch.setattr(core, '_try_playwright_fallback', fake_pw)
    monkeypatch.setattr(core, '_try_browser_fetch', fake_browser)
    return calls


def _browser_returning(monkeypatch, calls, text):
    def fake_browser(url, max_chars, reason='unknown'):
        calls['browser'].append(reason)
        return text
    monkeypatch.setattr(core, '_try_browser_fetch', fake_browser)


# ══════════════════════════════════════════════════════════
#  1. SPA shell (200 + too little extracted text)
# ══════════════════════════════════════════════════════════

def test_spa_shell_falls_to_browser(monkeypatch, harness):
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: None)
    monkeypatch.setattr(core, '_looks_like_spa_shell', lambda html, result: True)
    _browser_returning(monkeypatch, harness, _BROWSER_TEXT)

    out = core.fetch_page_content('https://spa-shell-1.example.com/app', max_chars=50000)

    assert out == _BROWSER_TEXT
    assert harness['browser'] == ['spa_shell']
    assert harness['pw'] == 1, 'Playwright must be tried before the browser'


def test_spa_shell_browser_empty_keeps_partial_shell_text(monkeypatch, harness):
    """Pre-seam behaviour: pw fails + extracted text > 50 chars → partial is returned."""
    partial = 'x' * 100
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: partial)
    monkeypatch.setattr(core, '_looks_like_spa_shell', lambda html, result: True)

    out = core.fetch_page_content('https://spa-shell-2.example.com/app', max_chars=50000)

    assert out == partial
    assert harness['browser'] == ['spa_shell'], 'browser must still be consulted first'


def test_spa_shell_browser_empty_no_text_returns_none(monkeypatch, harness):
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: None)
    monkeypatch.setattr(core, '_looks_like_spa_shell', lambda html, result: True)

    out = core.fetch_page_content('https://spa-shell-3.example.com/app', max_chars=50000)

    assert out is None
    assert harness['browser'] == ['spa_shell']


# ══════════════════════════════════════════════════════════
#  2. Bot/login wall (raw HTML and post-extraction text)
# ══════════════════════════════════════════════════════════

def test_bot_wall_html_falls_to_browser(monkeypatch, harness):
    monkeypatch.setattr(core, '_is_bot_protection', lambda html: True)
    _browser_returning(monkeypatch, harness, _BROWSER_TEXT)

    out = core.fetch_page_content('https://wall-1.example.com/', max_chars=50000)

    assert out == _BROWSER_TEXT
    assert harness['browser'] == ['login_wall']


def test_bot_text_falls_to_browser(monkeypatch, harness):
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: 'Please log in to continue' * 4)
    monkeypatch.setattr(core, '_is_bot_extracted_text', lambda text: True)
    _browser_returning(monkeypatch, harness, _BROWSER_TEXT)

    out = core.fetch_page_content('https://wall-2.example.com/', max_chars=50000)

    assert out == _BROWSER_TEXT
    assert harness['browser'] == ['login_wall']


def test_bot_text_browser_empty_returns_none(monkeypatch, harness):
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: 'login wall' * 20)
    monkeypatch.setattr(core, '_is_bot_extracted_text', lambda text: True)

    out = core.fetch_page_content('https://wall-3.example.com/', max_chars=50000)

    assert out is None
    assert harness['browser'] == ['login_wall']


# ══════════════════════════════════════════════════════════
#  3. Known-SPA domain whose anonymous render came back empty
# ══════════════════════════════════════════════════════════

def test_known_spa_falls_to_browser(monkeypatch, harness):
    monkeypatch.setattr(core, '_is_known_spa', lambda url: True)
    _browser_returning(monkeypatch, harness, _BROWSER_TEXT)

    out = core.fetch_page_content('https://known-spa-1.example.com/feed', max_chars=50000)

    assert out == _BROWSER_TEXT
    assert harness['browser'] == ['known_spa']
    assert harness['do_request'] == 0, 'known-SPA domains skip the plain GET'


# ══════════════════════════════════════════════════════════
#  4. Auth-source domain: the browser is the FIRST identity (0.7.0)
# ══════════════════════════════════════════════════════════

class _FakeAuthProvider:
    def __init__(self, row):
        self._row = row

    def match_source(self, url):
        return self._row

    def get_source(self, domain):
        return self._row


def _register_auth_row():
    row = {'domain': 'walled.example.com', 'cookies': [{'name': 'sess', 'value': 'stale'}]}
    register_auth_source_provider(_FakeAuthProvider(row))
    return row


def test_auth_source_browser_first_delivers(monkeypatch, harness):
    """Browser-first: the live session answers, the replay is never risked."""
    _register_auth_row()
    monkeypatch.setattr(core, '_try_authenticated_fetch',
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError('cookie replay must not fire when the browser delivers')))
    _browser_returning(monkeypatch, harness, _BROWSER_TEXT)

    out = core.fetch_page_content('https://walled.example.com/home', max_chars=50000)

    assert out == _BROWSER_TEXT
    assert harness['browser'] == ['auth_source_browser_first']
    assert harness['do_request'] == 0, (
        'a registered source means the domain is known login-walled; '
        'the anonymous GET must be skipped once the browser delivers')


def test_auth_source_browser_empty_falls_back_to_replay(monkeypatch, harness):
    """Browser has nothing → the stored-cookie replay is the fallback…"""
    _register_auth_row()
    replay_calls = []

    def fake_replay(url, src, mc, t):
        replay_calls.append(url)
        return 'content from the cookie replay fallback'

    monkeypatch.setattr(core, '_try_authenticated_fetch', fake_replay)
    monkeypatch.setattr(core, '_looks_like_login_wall', lambda text: False)

    out = core.fetch_page_content('https://walled.example.com/home', max_chars=50000)

    assert out == 'content from the cookie replay fallback'
    assert harness['browser'] == ['auth_source_browser_first']
    assert replay_calls == ['https://walled.example.com/home']
    assert harness['do_request'] == 0


def test_auth_source_all_identities_empty_continues_anonymous(monkeypatch, harness):
    """Browser empty + replay empty → the anonymous pipeline runs exactly as before."""
    _register_auth_row()
    monkeypatch.setattr(core, '_try_authenticated_fetch', lambda url, src, mc, t: None)
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: 'real public content' * 10)

    out = core.fetch_page_content('https://walled.example.com/public', max_chars=50000)

    assert out == 'real public content' * 10
    assert harness['browser'] == ['auth_source_browser_first']
    assert harness['do_request'] == 1, 'anonymous pipeline must still run when every identity path yields nothing'


# ══════════════════════════════════════════════════════════
#  5. Over-trigger guard: Playwright success ⇒ browser never consulted
# ══════════════════════════════════════════════════════════

def test_playwright_success_never_touches_browser(monkeypatch, harness):
    monkeypatch.setattr(core, '_extract_html_text', lambda html, limit, url=None: None)
    monkeypatch.setattr(core, '_looks_like_spa_shell', lambda html, result: True)
    monkeypatch.setattr(core, '_try_playwright_fallback',
                        lambda url, max_chars, timeout: 'rendered by server playwright')

    out = core.fetch_page_content('https://spa-shell-4.example.com/app', max_chars=50000)

    assert out == 'rendered by server playwright'
    assert harness['browser'] == [], 'browser is the LAST identity path, not a first resort'

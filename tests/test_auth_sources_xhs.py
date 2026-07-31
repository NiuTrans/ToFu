"""Offline unit tests for the authenticated-fetch source layer + XHS engine.

Covers:
  * lib/auth_sources.py — cookie-header parse, storage-state extract, domain
    normalisation, match_source gating (enabled + cookie-bearing only),
    upsert / toggle / delete (default reset), redaction (no secret leak).
  * lib/fetch/core.py — fetch_page_content routes a matched URL through the
    authenticated path BEFORE the anonymous pipeline.
  * lib/search/engines/xhs.py — availability gate, result normalisation,
    garbage filtering, source mapping.
  * lib/search/orchestrator.py — XHS appears in the engine wiring only when
    the source is connected.

No network / no browser: the Playwright pool methods are monkeypatched. Each
test fully cleans up the auth-source it creates so state never leaks across
tests (the store persists to data/config/auth_sources.json).
"""

import pytest

import lib.auth_sources as A


class _AuthSourceProviderForTests:
    """Bridge tofu_search's auth-source seam to chatui's lib.auth_sources.

    After the search/fetch extraction the XHS engine and the authenticated
    fetch path read connected sources through ``tofu_search.providers``
    (the host registers a provider), not by importing the host store
    directly. Production wires this in ``lib/search_bridge.py``; tests do the
    same so the provider-gated paths are exercised against the real store.
    """

    def match_source(self, url):
        return A.match_source(url)

    def get_source(self, domain):
        return A.get_source(domain)


@pytest.fixture(autouse=True)
def _clean_xhs_source():
    """Ensure xiaohongshu.com is reset (disabled, no cookies) around each test,
    and the tofu_search auth-source provider is registered for the duration."""
    import tofu_search

    A.delete_source('xiaohongshu.com')
    tofu_search.register_auth_source_provider(_AuthSourceProviderForTests())
    yield
    tofu_search.register_auth_source_provider(None)
    A.delete_source('xiaohongshu.com')


# ═══════════════════════════════════════════════════════════
#  auth_sources — parsing + normalisation
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAuthSourceParsing:
    def test_normalize_domain(self):
        assert A.normalize_domain('https://www.Xiaohongshu.com/explore') == 'xiaohongshu.com'
        assert A.normalize_domain('WWW.Example.COM:8080') == 'example.com'
        assert A.normalize_domain('user@host.com/path') == 'host.com'
        assert A.normalize_domain('') == ''

    def test_parse_cookie_header_basic(self):
        # Scope is HOST-ONLY (no leading dot). A `Cookie:` header carries no
        # scope information at all, and stamping '.'+domain onto every entry
        # silently broke host-only session cookies: a browser treats
        # '.internal.example.com' as a DIFFERENT cookie from 'your-llm-gateway.example.com', so the
        # site's auth probe never received it and the authenticated fetch landed
        # on the login wall — while the store still reported "connected".
        # Host-only is the narrower and therefore safer default; a parent-domain
        # cookie must now be asked for explicitly (per-cookie `domain`).
        # Full invariant set: tests/test_auth_sources_cookie_domain.py
        out = A.parse_cookie_header('web_session=abc; a1=xyz', 'xiaohongshu.com')
        assert out == [
            {'name': 'web_session', 'value': 'abc', 'domain': 'xiaohongshu.com', 'path': '/'},
            {'name': 'a1', 'value': 'xyz', 'domain': 'xiaohongshu.com', 'path': '/'},
        ]

    def test_parse_cookie_header_strips_prefix_and_blanks(self):
        out = A.parse_cookie_header('Cookie: k=v;; =noname; bad', 'x.com')
        # only the valid k=v pair survives
        assert out == [{'name': 'k', 'value': 'v', 'domain': 'x.com', 'path': '/'}]

    def test_parse_cookie_header_empty(self):
        assert A.parse_cookie_header('', 'x.com') == []
        assert A.parse_cookie_header('   ', 'x.com') == []

    def test_cookies_from_storage_state_filters_domain(self):
        state = {'cookies': [
            {'name': 'a', 'value': '1', 'domain': '.xiaohongshu.com'},
            {'name': 'b', 'value': '2', 'domain': 'other.com'},
            {'name': 'c', 'value': '3', 'domain': 'www.xiaohongshu.com'},
        ]}
        out = A.cookies_from_storage_state(state, 'xiaohongshu.com')
        names = {c['name'] for c in out}
        assert names == {'a', 'c'}

    def test_cookies_from_storage_state_no_filter(self):
        state = {'cookies': [{'name': 'a', 'value': '1', 'domain': 'x.com'}]}
        assert len(A.cookies_from_storage_state(state)) == 1
        assert A.cookies_from_storage_state({}) == []
        assert A.cookies_from_storage_state('notadict') == []


# ═══════════════════════════════════════════════════════════
#  auth_sources — store + matching
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestAuthSourceStore:
    def test_default_source_present_disabled(self):
        rows = {r['domain']: r for r in A.list_sources()}
        assert 'xiaohongshu.com' in rows
        assert rows['xiaohongshu.com']['enabled'] is False
        assert rows['xiaohongshu.com']['has_cookies'] is False

    def test_match_requires_enabled_and_cookies(self):
        # No cookies yet → never matches even if we enable it.
        A.set_enabled('xiaohongshu.com', True)
        assert A.match_source('https://www.xiaohongshu.com/explore/1') is None

        # Add cookies + enable → matches, including alias + subdomain.
        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)
        assert A.match_source('https://www.xiaohongshu.com/explore/1') is not None
        assert A.match_source('https://xhslink.com/abc') is not None  # alias
        assert A.match_source('https://example.com/') is None

        # Disable → stops matching.
        A.set_enabled('xiaohongshu.com', False)
        assert A.match_source('https://www.xiaohongshu.com/explore/1') is None

    def test_match_disabled_when_cookies_but_not_enabled(self):
        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=False)
        assert A.match_source('https://www.xiaohongshu.com/explore/1') is None

    def test_list_sources_redacts_secrets(self):
        A.upsert_source('xiaohongshu.com', cookie_header='web_session=supersecret',
                        proxy='http://user:pass@host:3128', enabled=True)
        rows = {r['domain']: r for r in A.list_sources()}
        row = rows['xiaohongshu.com']
        # No raw cookie list / value, no proxy creds.
        assert 'cookies' not in row
        assert 'proxy' not in row
        assert row['cookie_count'] == 1
        assert row['has_cookies'] is True
        assert row['has_proxy'] is True
        assert row['proxy_hint'] == 'host'  # hostname only, no user:pass
        import json
        assert 'supersecret' not in json.dumps(rows)

    def test_get_source_returns_full_cookies(self):
        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)
        src = A.get_source('xiaohongshu.com')
        assert src['cookies'][0]['value'] == 'tok'

    def test_delete_default_resets_not_removes(self):
        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)
        assert A.delete_source('xiaohongshu.com') is True
        rows = {r['domain']: r for r in A.list_sources()}
        # Default domain still listed, but reset.
        assert 'xiaohongshu.com' in rows
        assert rows['xiaohongshu.com']['enabled'] is False
        assert rows['xiaohongshu.com']['has_cookies'] is False

    def test_toggle_unknown_returns_false(self):
        assert A.set_enabled('definitely-not-a-source.example', True) is False


# ═══════════════════════════════════════════════════════════
#  auth_sources — structured cookie fields (the UI's write path)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestStructuredCookieFields:
    """The Settings UI collects ONE input per cookie instead of a single
    free-text ``name=value; name=value`` blob. These pin the contract that
    makes that possible: a server-declared field spec, a structured writer,
    and a refusal to store a credential set that cannot authenticate."""

    def test_spec_declares_fields_and_login_url(self):
        spec = A.source_spec('https://www.xiaohongshu.com/explore')  # normalises
        assert spec['login_url'].startswith('https://')
        names = [f['name'] for f in A.source_fields('xiaohongshu.com')]
        assert names[:2] == ['web_session', 'a1']
        imp = {f['name']: f['importance'] for f in A.source_fields('xiaohongshu.com')}
        assert imp['web_session'] == 'required'
        assert imp['a1'] == 'recommended'
        # Unknown domain → empty spec, never a crash.
        assert A.source_spec('nope.example') == {}
        assert A.source_fields('nope.example') == []

    def test_cookies_from_fields_builds_playwright_shape(self):
        # Host-only default — see test_parse_cookie_header_basic for why the
        # previous '.'+domain behaviour was a defect, not a convention.
        out = A.cookies_from_fields(
            {'web_session': 'abc', 'a1': 'xyz'}, 'xiaohongshu.com')
        assert out == [
            {'name': 'web_session', 'value': 'abc', 'domain': 'xiaohongshu.com', 'path': '/'},
            {'name': 'a1', 'value': 'xyz', 'domain': 'xiaohongshu.com', 'path': '/'},
        ]

    def test_cookies_from_fields_drops_blanks_and_garbage(self):
        # An untouched optional input is not an instruction to store ''.
        out = A.cookies_from_fields(
            {'web_session': 'tok', 'a1': '', 'webId': '   '}, 'xiaohongshu.com')
        assert [c['name'] for c in out] == ['web_session']
        assert A.cookies_from_fields('notadict', 'x.com') == []
        assert A.cookies_from_fields({}, 'x.com') == []

    def test_cookies_from_fields_trims_pasted_whitespace(self):
        out = A.cookies_from_fields({'web_session': '  tok  '}, 'xiaohongshu.com')
        assert out[0]['value'] == 'tok'

    def test_upsert_via_fields_connects(self):
        A.upsert_source('xiaohongshu.com',
                        cookie_fields={'web_session': 'tok', 'a1': 'aaa'},
                        enabled=True)
        src = A.get_source('xiaohongshu.com')
        assert {c['name'] for c in src['cookies']} == {'web_session', 'a1'}
        assert A.match_source('https://www.xiaohongshu.com/explore/1') is not None

    def test_upsert_rejects_missing_required_cookie(self):
        """The old free-text box stored a mistyped paste and then reported
        '已连接'; the failure only surfaced later as an empty fetch."""
        with pytest.raises(ValueError) as ei:
            A.upsert_source('xiaohongshu.com',
                            cookie_fields={'a1': 'aaa'}, enabled=True)
        assert 'web_session' in str(ei.value)
        # Nothing was stored — the source stays disconnected, not half-connected.
        rows = {r['domain']: r for r in A.list_sources()}
        assert rows['xiaohongshu.com']['has_cookies'] is False

    def test_upsert_rejects_header_missing_required_cookie(self):
        """Same gate on the raw-header path — validation lives in the store,
        not in whichever caller happens to be fashionable."""
        with pytest.raises(ValueError):
            A.upsert_source('xiaohongshu.com', cookie_header='a1=aaa', enabled=True)

    def test_unknown_domain_has_no_required_gate(self):
        """A site with no declared spec must stay usable — the gate is driven
        by the catalog, not by a blanket assumption about cookie names."""
        try:
            A.upsert_source('example.test', cookie_fields={'sid': 'x'}, enabled=True)
            assert A.get_source('example.test')['cookies'][0]['name'] == 'sid'
        finally:
            A.delete_source('example.test')

    def test_missing_required_fields_helper(self):
        assert A.missing_required_fields(
            [{'name': 'web_session', 'value': 'v'}], 'xiaohongshu.com') == []
        # Present-but-blank counts as missing, not as satisfied.
        assert A.missing_required_fields(
            [{'name': 'web_session', 'value': '  '}], 'xiaohongshu.com') == ['web_session']
        assert A.missing_required_fields([], 'nope.example') == []

    def test_list_sources_carries_spec_for_the_ui(self):
        """The UI renders its inputs from THIS payload, so a site's cookie
        list is declared once (server-side) rather than duplicated in JS."""
        row = {r['domain']: r for r in A.list_sources()}['xiaohongshu.com']
        assert row['login_url'].startswith('https://')
        assert [f['name'] for f in row['fields']][:2] == ['web_session', 'a1']


# ═══════════════════════════════════════════════════════════
#  fetch routing — authenticated path takes priority
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestFetchRouting:
    def test_matched_url_routes_to_authenticated_fetch(self, monkeypatch):
        import tofu_search.fetch.core as core
        import tofu_search.fetch.playwright_pool as pp

        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)

        calls = {}

        def fake_auth(url, cookies, proxy='', timeout=25, max_chars=None):
            calls['url'] = url
            calls['cookies'] = cookies
            return ('NOTE BODY: a sufficiently long xiaohongshu note body to clear '
                    'the fifty character minimum threshold cleanly.')

        monkeypatch.setattr(pp._pw_pool, 'fetch_authenticated', fake_auth)

        # Anonymous path must NOT run for a matched source.
        def boom(*a, **k):
            raise AssertionError('anonymous path should not run for a matched auth source')

        monkeypatch.setattr(core, '_do_request', boom)

        out = core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')
        assert calls['url'].endswith('/explore/abc')
        assert calls['cookies'][0]['name'] == 'web_session'
        assert out.startswith('NOTE BODY')

    def test_unmatched_url_skips_authenticated_fetch(self, monkeypatch):
        import tofu_search.fetch.core as core
        import tofu_search.fetch.playwright_pool as pp

        # Source NOT connected → no auth fetch even for xiaohongshu URL.
        def boom_auth(*a, **k):
            raise AssertionError('auth fetch must not run when source disconnected')

        monkeypatch.setattr(pp._pw_pool, 'fetch_authenticated', boom_auth)
        # Make the anonymous path return a sentinel quickly.
        monkeypatch.setattr(core, '_do_request',
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('stop')))
        # We only assert auth fetch wasn't called; the anonymous path raising
        # internally is caught by fetch_page_content and returns ''.
        core.fetch_page_content('https://www.xiaohongshu.com/explore/abc')


# ═══════════════════════════════════════════════════════════
#  XHS search engine
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestXhsEngine:
    def test_unavailable_when_disconnected(self):
        from tofu_search.search.engines.xhs import search_xhs, xhs_search_available
        assert xhs_search_available() is False
        # Returns [] without touching the pool.
        assert search_xhs('ramen') == []

    def test_available_and_normalises_results(self, monkeypatch):
        from tofu_search.search.engines import xhs
        import tofu_search.fetch.playwright_pool as pp

        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)
        assert xhs.xhs_search_available() is True

        def fake_search(url, cookies, proxy='', timeout=20, extractor_js='[]', wait_selector=''):
            assert 'search_result?keyword=' in url
            assert cookies[0]['name'] == 'web_session'
            return [
                {'title': '  best ramen 拉面  ', 'snippet': 'userA · 100',
                 'url': 'https://www.xiaohongshu.com/explore/abc'},
                {'title': '', 'url': 'https://www.xiaohongshu.com/explore/x'},  # dropped
                {'title': 'no url'},                                            # dropped
                'garbage',                                                      # dropped
            ]

        monkeypatch.setattr(pp._pw_pool, 'search_authenticated', fake_search)
        out = xhs.search_xhs('ramen', max_results=10)
        assert len(out) == 1
        assert out[0]['title'] == 'best ramen 拉面'
        assert out[0]['source'] == 'Xiaohongshu'
        assert out[0]['url'].endswith('/explore/abc')

    def test_respects_max_results(self, monkeypatch):
        from tofu_search.search.engines import xhs
        import tofu_search.fetch.playwright_pool as pp

        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)

        def fake_search(url, cookies, **k):
            return [{'title': f'note {i}', 'url': f'https://www.xiaohongshu.com/explore/{i}'}
                    for i in range(20)]

        monkeypatch.setattr(pp._pw_pool, 'search_authenticated', fake_search)
        assert len(xhs.search_xhs('q', max_results=5)) == 5

    def test_empty_pool_result(self, monkeypatch):
        from tofu_search.search.engines import xhs
        import tofu_search.fetch.playwright_pool as pp

        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)
        monkeypatch.setattr(pp._pw_pool, 'search_authenticated', lambda *a, **k: None)
        assert xhs.search_xhs('q') == []


# ═══════════════════════════════════════════════════════════
#  orchestrator wiring
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestOrchestratorWiring:
    def test_xhs_imported_and_gated(self):
        import inspect

        from tofu_search.search import orchestrator as o
        src = inspect.getsource(o.perform_web_search)
        # Conditionally wired on availability, not unconditionally.
        assert 'xhs_search_available' in src
        assert 'Xiaohongshu' in src

    def test_engine_available_helper(self):
        from tofu_search.search.engines.xhs import xhs_search_available
        assert xhs_search_available() is False
        A.upsert_source('xiaohongshu.com', cookie_header='web_session=tok', enabled=True)
        assert xhs_search_available() is True

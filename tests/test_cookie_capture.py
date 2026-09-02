"""Behaviour tests for lib/browser/cookie_capture.py + the fetch.py wall hook.

Contract (epic pt_c009ff1c36ba4527; auto-capture per owner decision 2026-08-13 —
the consent banner / grant store / resolve endpoints were removed):
  * wall detection is netloc-based (the SSO login URL carries the original
    page as redirect_uri — whole-URL substring matching misclassifies);
  * capture engages AUTOMATICALLY on a wall — but cookies are stored ONLY
    after a probe proves the page no longer walls, so anonymous tracking
    cookies are never mistaken for a session;
  * every capture is audit-logged with cookie COUNT, never values;
  * a fresh stored session suppresses re-capture (no capture loop);
  * a per-domain cooldown suppresses a second login tab right after one was
    opened (no tab-spam when the user ignores the login tab);
  * the fetch hook returns None on a wall (wall text is not content) and
    retries inline only when capture completed synchronously.

NEUTER anchors:
  * test_anonymous_cookies_not_stored_without_probe_pass — removing the
    probe-verify must turn this red (anonymous cookies would be stored);
  * test_capture_is_audited                        — removing audit_log red.

All offline: extension commands, probes, and push are faked.
"""

import time

import pytest

import lib.browser.cookie_capture as cc

pytestmark = pytest.mark.unit
CLIENT_ID = 'test-browser'
USER_ID = '41'


@pytest.fixture(autouse=True)
def _isolated_state():
    with cc._capture_lock:
        cc._capture_threads.clear()
        cc._last_attempt.clear()
    yield
    with cc._capture_lock:
        cc._capture_threads.clear()
        cc._last_attempt.clear()


@pytest.fixture
def ext_online(monkeypatch):
    def fake_connected(client_id, *, owner_user_id):
        return client_id == CLIENT_ID and owner_user_id == USER_ID

    monkeypatch.setattr('lib.browser.queue.is_extension_connected',
                        fake_connected)


@pytest.fixture
def no_existing_source(monkeypatch):
    monkeypatch.setattr('lib.auth_sources.match_source', lambda url: None)


# ══════════════════════════════════════════════════════════
#  1. Wall detection
# ══════════════════════════════════════════════════════════

class TestWallDetection:
    def test_sso_host_redirect_is_wall(self):
        assert cc.looks_like_login_wall(
            'https://api.openai.com/ml/modelPlaza/modelInfo?x=1',
            'https://ssosv.internal.example.com/sson/login?client_id=12d702aa62'
            '&redirect_uri=https%3A%2F%2Fyour-llm-gateway.example.com%2Fsso%2Fcallback',
            '统一登录中心')

    def test_same_origin_redirect_is_not_wall(self):
        assert not cc.looks_like_login_wall(
            'https://example.com/a',
            'https://www.example.com/a', 'Example')

    def test_cross_domain_without_login_markers_is_not_wall(self):
        assert not cc.looks_like_login_wall(
            'https://t.co/abc',
            'https://cdn.other-site.com/article/123', 'Some Article')

    def test_same_host_login_path_is_wall(self):
        assert cc.looks_like_login_wall(
            'https://site.com/dashboard',
            'https://site.com/login?next=/dashboard', '请登录')

    def test_same_host_content_page_is_not_wall(self):
        assert not cc.looks_like_login_wall(
            'https://site.com/a',
            'https://site.com/b?utm=1', 'Login to our newsletter and win!')

    def test_cross_domain_login_title_is_wall(self):
        assert cc.looks_like_login_wall(
            'https://site.com/app',
            'https://accounts.other-idp.com/page?r=1', '请登录')


# ══════════════════════════════════════════════════════════
#  2. Capture orchestration (auto-approved — no consent gate)
# ══════════════════════════════════════════════════════════

class TestCapture:
    def test_capture_stores_only_after_probe_clears_wall(
            self, monkeypatch, ext_online, no_existing_source):
        stored = {}
        monkeypatch.setattr('lib.auth_sources.upsert_source',
                            lambda dom, **kw: stored.update(domain=dom, **kw) or {})
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        monkeypatch.setattr(
            cc, '_probe_no_longer_walled',
            lambda url, *, client_id, owner_user_id: (
                client_id == CLIENT_ID and owner_user_id == USER_ID))
        monkeypatch.setattr(cc, '_fetch_cookies',
                            lambda dom, *, client_id, owner_user_id: (
                                [{'name': 'sess', 'value': 'x'}]
                                if (client_id, owner_user_id)
                                == (CLIENT_ID, USER_ID) else []))
        monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)

        assert cc.handle_login_wall(
            'https://walled.example.com/app',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is True
        assert stored.get('domain') == 'walled.example.com'
        assert stored.get('enabled') is True
        assert stored.get('cookies') == [{'name': 'sess', 'value': 'x'}]

    def test_anonymous_cookies_not_stored_without_probe_pass(
            self, monkeypatch, ext_online, no_existing_source):
        """NEUTER anchor for the probe-verify: get_cookies returns anonymous
        cookies but the page STILL walls → nothing may be stored, and the
        async login-tab path engages instead."""
        upsert_calls = []
        monkeypatch.setattr('lib.auth_sources.upsert_source',
                            lambda dom, **kw: upsert_calls.append(dom) or {})
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        monkeypatch.setattr(
            cc, '_probe_no_longer_walled',
            lambda url, *, client_id, owner_user_id: False)
        monkeypatch.setattr(cc, '_fetch_cookies',
                            lambda dom, *, client_id, owner_user_id: [
                                {'name': '_track', 'value': 'anon'}])
        monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
        started = []
        monkeypatch.setattr(cc.threading, 'Thread',
                            lambda **kw: started.append(kw) or
                            type('T', (), {'start': lambda self: None})())

        assert cc.handle_login_wall(
            'https://walled.example.com/app',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is False
        assert upsert_calls == [], 'anonymous cookies must never be stored'
        assert started, 'async capture should engage for a still-walled page'

    def test_capture_is_audited(self, monkeypatch, ext_online, no_existing_source):
        audits = []
        monkeypatch.setattr(cc, 'audit_log',
                            lambda event, **kw: audits.append((event, kw)))
        monkeypatch.setattr('lib.auth_sources.upsert_source', lambda dom, **kw: {})
        monkeypatch.setattr(
            cc, '_probe_no_longer_walled',
            lambda url, *, client_id, owner_user_id: True)
        monkeypatch.setattr(cc, '_fetch_cookies',
                            lambda dom, *, client_id, owner_user_id: [
                                {'name': 'a', 'value': '1'},
                                {'name': 'b', 'value': '2'}])
        monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)

        assert cc.handle_login_wall(
            'https://walled.example.com/',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is True
        capture_events = [kw for ev, kw in audits if ev == 'cookie_capture']
        assert len(capture_events) == 1
        assert capture_events[0]['cookie_count'] == 2
        assert capture_events[0]['domain'] == 'walled.example.com'
        assert 'value' not in str(capture_events[0]), 'cookie values must not be audited'

    def test_login_tab_cooldown_suppresses_second_attempt(
            self, monkeypatch, ext_online, no_existing_source):
        """A login tab that the user ignores must NOT re-open on every fetch
        round: within _ATTEMPT_COOLDOWN_S a second wall only logs a skip."""
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        monkeypatch.setattr(
            cc, '_probe_no_longer_walled',
            lambda url, *, client_id, owner_user_id: False)
        monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)
        started = []
        monkeypatch.setattr(cc.threading, 'Thread',
                            lambda **kw: started.append(kw) or
                            type('T', (), {'start': lambda self: None})())

        assert cc.handle_login_wall(
            'https://walled.example.com/app',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is False
        assert len(started) == 1

        # Simulate the capture thread having exited; the cooldown remains.
        with cc._capture_lock:
            cc._capture_threads.pop(
                (USER_ID, CLIENT_ID, 'walled.example.com'), None)
        assert cc.handle_login_wall(
            'https://walled.example.com/app',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is False
        assert len(started) == 1, 'a second login tab must not open inside the cooldown'

    def test_fresh_auth_source_suppresses_recapture(
            self, monkeypatch, ext_online):
        monkeypatch.setattr('lib.auth_sources.match_source',
                            lambda url: {'domain': 'walled.example.com',
                                         'updated_at': time.time()})
        probe_calls = []
        monkeypatch.setattr(cc, '_probe_no_longer_walled',
                            lambda url, **route: probe_calls.append(url) or True)
        assert cc.handle_login_wall(
            'https://walled.example.com/',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is False
        assert probe_calls == []

    def test_offline_extension_noop(self, monkeypatch):
        monkeypatch.setattr('lib.browser.queue.is_extension_connected',
                            lambda *a, **k: False)
        probe_calls = []
        monkeypatch.setattr(cc, '_probe_no_longer_walled',
                            lambda url, **route: probe_calls.append(url) or True)
        assert cc.handle_login_wall(
            'https://walled.example.com/',
            client_id=CLIENT_ID,
            user_id=USER_ID,
        ) is False
        assert probe_calls == []


# ══════════════════════════════════════════════════════════
#  3. fetch.py hook
# ══════════════════════════════════════════════════════════

class TestFetchHook:
    def _prime(self, monkeypatch, first_result, captured=False, retry_result=None):
        import lib.browser.fetch as bfetch
        calls = []

        def fake_send(
                cmd, params, timeout=30, client_id=None,
                owner_user_id=None):
            assert (client_id, owner_user_id) == (CLIENT_ID, USER_ID)
            calls.append(cmd)
            if len(calls) == 1:
                return first_result, None
            return retry_result, None

        monkeypatch.setattr(bfetch, 'send_browser_command', fake_send)
        monkeypatch.setattr(
            bfetch, 'is_extension_connected',
            lambda client_id, *, owner_user_id: (
                client_id == CLIENT_ID and owner_user_id == USER_ID))
        monkeypatch.setattr(
            'lib.browser.protocol.require_capabilities',
            lambda client_id, required: {'client_id': client_id},
        )
        engaged = []

        def fake_capture(
                url, final_url='', *, client_id, user_id):
            assert (client_id, user_id) == (CLIENT_ID, USER_ID)
            engaged.append(url)
            return captured

        monkeypatch.setattr(
            'lib.browser.cookie_capture.handle_login_wall', fake_capture)
        return bfetch, calls, engaged

    def test_walled_result_returns_none_and_engages_capture(self, monkeypatch):
        bfetch, calls, engaged = self._prime(monkeypatch, {
            'url': 'https://ssosv.internal.example.com/sson/login?client_id=x',
            'title': '统一登录中心',
            'text': '二维码登录 简体中文 登录您的账号 ' * 20,
        })
        out = bfetch.fetch_url_via_browser(
            'https://api.openai.com/ml/modelPlaza/modelInfo',
            client_id=CLIENT_ID,
            owner_user_id=USER_ID,
        )
        assert out is None, 'wall text must not be served as content'
        assert engaged == ['https://api.openai.com/ml/modelPlaza/modelInfo']
        assert calls == ['fetch_url'], 'no inline retry when capture did not complete'

    def test_captured_retries_inline(self, monkeypatch):
        bfetch, calls, engaged = self._prime(
            monkeypatch,
            {'url': 'https://ssosv.internal.example.com/sson/login', 'title': '登录',
             'text': 'wall ' * 100},
            captured=True,
            retry_result={'url': 'https://api.openai.com/ml/modelPlaza/modelInfo',
                          'title': 'FRIDAY', 'text': 'real model list ' * 50})
        out = bfetch.fetch_url_via_browser(
            'https://api.openai.com/ml/modelPlaza/modelInfo',
            client_id=CLIENT_ID,
            owner_user_id=USER_ID,
        )
        assert calls == ['fetch_url', 'fetch_url'], 'one inline retry after capture'
        assert out is not None and 'real model list' in out

    def test_non_wall_result_untouched(self, monkeypatch):
        bfetch, calls, engaged = self._prime(monkeypatch, {
            'url': 'https://example.com/article',
            'title': 'A normal page',
            'text': 'perfectly fine content ' * 50,
        })
        out = bfetch.fetch_url_via_browser(
            'https://example.com/article',
            client_id=CLIENT_ID,
            owner_user_id=USER_ID,
        )
        assert out is not None
        assert engaged == []

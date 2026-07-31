"""Behaviour tests for lib/browser/cookie_capture.py + the fetch.py wall hook.

Contract (epic pt_c009ff1c36ba4527):
  * wall detection is netloc-based (the SSO login URL carries the original
    page as redirect_uri — whole-URL substring matching misclassifies);
  * NO capture without a recorded per-domain grant (one-time consent);
  * a denial suppresses re-asking for a cooldown (no nag-every-fetch);
  * cookies are stored ONLY after a probe proves the page no longer walls —
    anonymous tracking cookies must never be mistaken for a session;
  * every capture is audit-logged with cookie COUNT, never values;
  * a fresh stored session suppresses re-capture (no capture loop);
  * the fetch hook returns None on a wall (wall text is not content) and
    retries inline only when capture completed synchronously.

NEUTER anchors:
  * test_no_capture_without_consent            — removing the consent gate
    must turn this red (capture would fire unconsented);
  * test_anonymous_cookies_not_stored_without_probe_pass — removing the
    probe-verify must turn this red (anonymous cookies would be stored);
  * test_capture_is_audited                    — removing audit_log red.

All offline: extension commands, probes, and push are faked.
"""

import threading
import time

import pytest

import lib.browser.cookie_capture as cc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, '_CONSENT_PATH', str(tmp_path / 'consent.json'))
    monkeypatch.setattr(cc, '_CONSENT_TIMEOUT_S', 5)
    monkeypatch.setattr(cc, '_IMMEDIATE_WAIT_S', 1)
    with cc._pending_lock:
        cc._pending.clear()
        cc._capture_threads.clear()
    yield
    with cc._pending_lock:
        cc._pending.clear()
        cc._capture_threads.clear()


@pytest.fixture
def ext_online(monkeypatch):
    monkeypatch.setattr('lib.browser.queue.is_extension_connected',
                        lambda *a, **k: True)


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
#  2. Consent gate
# ══════════════════════════════════════════════════════════

class TestConsentGate:
    def test_grant_persisted_and_not_reasked(self, monkeypatch):
        pushes = []
        monkeypatch.setattr('lib.push.push_event',
                            lambda ch, tid, payload: pushes.append(payload))
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)

        def approver():
            time.sleep(0.05)
            pend = cc.pending_consents()
            assert len(pend) == 1
            cc.resolve_consent(pend[0]['id'], True)

        threading.Thread(target=approver, daemon=True).start()
        assert cc.request_consent('example.com', 'https://example.com/x') is True
        assert cc._grant_for('example.com')
        assert pushes and pushes[0]['type'] == 'request'

        # One-time: a second request never pushes nor blocks.
        pushes.clear()
        t0 = time.time()
        assert cc._grant_for('example.com') is True
        assert time.time() - t0 < 1
        assert pushes == []

    def test_denial_suppresses_reask_within_cooldown(self, monkeypatch):
        pushes = []
        monkeypatch.setattr('lib.push.push_event',
                            lambda ch, tid, payload: pushes.append(payload))
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)

        def denier():
            time.sleep(0.05)
            cc.resolve_consent(cc.pending_consents()[0]['id'], False)

        threading.Thread(target=denier, daemon=True).start()
        assert cc.request_consent('example.com', 'https://example.com/x') is False
        assert cc._denial_fresh('example.com')

    def test_revoke_forces_reask(self, monkeypatch):
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        cc._record_grant('example.com')
        assert cc._grant_for('example.com')
        assert cc.revoke_consent('example.com') is True
        assert not cc._grant_for('example.com')
        assert cc.revoke_consent('example.com') is False


# ══════════════════════════════════════════════════════════
#  3. Capture orchestration
# ══════════════════════════════════════════════════════════

class TestCapture:
    def _grant(self):
        cc._record_grant('walled.example.com')

    def test_capture_stores_only_after_probe_clears_wall(
            self, monkeypatch, ext_online, no_existing_source):
        self._grant()
        stored = {}
        monkeypatch.setattr('lib.auth_sources.upsert_source',
                            lambda dom, **kw: stored.update(domain=dom, **kw) or {})
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        monkeypatch.setattr(cc, '_probe_no_longer_walled', lambda url: True)
        monkeypatch.setattr(cc, '_fetch_cookies',
                            lambda dom: [{'name': 'sess', 'value': 'x'}])
        monkeypatch.setattr('lib.push.push_event', lambda *a, **k: None)

        assert cc.handle_login_wall('https://walled.example.com/app') is True
        assert stored.get('domain') == 'walled.example.com'
        assert stored.get('enabled') is True
        assert stored.get('cookies') == [{'name': 'sess', 'value': 'x'}]

    def test_anonymous_cookies_not_stored_without_probe_pass(
            self, monkeypatch, ext_online, no_existing_source):
        """NEUTER anchor for the probe-verify: get_cookies returns anonymous
        cookies but the page STILL walls → nothing may be stored, and the
        async login-tab path engages instead."""
        self._grant()
        upsert_calls = []
        monkeypatch.setattr('lib.auth_sources.upsert_source',
                            lambda dom, **kw: upsert_calls.append(dom) or {})
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        monkeypatch.setattr(cc, '_probe_no_longer_walled', lambda url: False)
        monkeypatch.setattr(cc, '_fetch_cookies',
                            lambda dom: [{'name': '_track', 'value': 'anon'}])
        monkeypatch.setattr('lib.push.push_event', lambda *a, **k: None)
        started = []
        monkeypatch.setattr(cc.threading, 'Thread',
                            lambda **kw: started.append(kw) or
                            type('T', (), {'start': lambda self: None})())

        assert cc.handle_login_wall('https://walled.example.com/app') is False
        assert upsert_calls == [], 'anonymous cookies must never be stored'
        assert started, 'async capture should engage for a still-walled page'

    def test_capture_is_audited(self, monkeypatch, ext_online, no_existing_source):
        self._grant()
        audits = []
        monkeypatch.setattr(cc, 'audit_log',
                            lambda event, **kw: audits.append((event, kw)))
        monkeypatch.setattr('lib.auth_sources.upsert_source', lambda dom, **kw: {})
        monkeypatch.setattr(cc, '_probe_no_longer_walled', lambda url: True)
        monkeypatch.setattr(cc, '_fetch_cookies',
                            lambda dom: [{'name': 'a', 'value': '1'},
                                         {'name': 'b', 'value': '2'}])
        monkeypatch.setattr('lib.push.push_event', lambda *a, **k: None)

        assert cc.handle_login_wall('https://walled.example.com/') is True
        capture_events = [kw for ev, kw in audits if ev == 'cookie_capture']
        assert len(capture_events) == 1
        assert capture_events[0]['cookie_count'] == 2
        assert capture_events[0]['domain'] == 'walled.example.com'
        assert 'value' not in str(capture_events[0]), 'cookie values must not be audited'

    def test_no_capture_without_consent(
            self, monkeypatch, ext_online, no_existing_source):
        """NEUTER anchor for the consent gate: no grant + instant denial →
        the store/probe/upsert path must never run."""
        monkeypatch.setattr('lib.push.push_event', lambda *a, **k: None)
        monkeypatch.setattr(cc, 'audit_log', lambda *a, **k: None)
        monkeypatch.setattr(cc, 'request_consent', lambda dom, url: False)
        probe_calls = []
        monkeypatch.setattr(cc, '_probe_no_longer_walled',
                            lambda url: probe_calls.append(url) or True)
        upsert_calls = []
        monkeypatch.setattr('lib.auth_sources.upsert_source',
                            lambda dom, **kw: upsert_calls.append(dom) or {})

        assert cc.handle_login_wall('https://walled.example.com/') is False
        assert probe_calls == [], 'probe must not run without consent'
        assert upsert_calls == [], 'nothing may be stored without consent'

    def test_fresh_auth_source_suppresses_recapture(
            self, monkeypatch, ext_online):
        self._grant()
        monkeypatch.setattr('lib.auth_sources.match_source',
                            lambda url: {'domain': 'walled.example.com',
                                         'updated_at': time.time()})
        probe_calls = []
        monkeypatch.setattr(cc, '_probe_no_longer_walled',
                            lambda url: probe_calls.append(url) or True)
        assert cc.handle_login_wall('https://walled.example.com/') is False
        assert probe_calls == []

    def test_offline_extension_noop(self, monkeypatch):
        monkeypatch.setattr('lib.browser.queue.is_extension_connected',
                            lambda *a, **k: False)
        consent_calls = []
        monkeypatch.setattr(cc, 'request_consent',
                            lambda dom, url: consent_calls.append(dom) or True)
        assert cc.handle_login_wall('https://walled.example.com/') is False
        assert consent_calls == []


# ══════════════════════════════════════════════════════════
#  4. fetch.py hook
# ══════════════════════════════════════════════════════════

class TestFetchHook:
    def _prime(self, monkeypatch, first_result, captured=False, retry_result=None):
        import lib.browser.fetch as bfetch
        calls = []

        def fake_send(cmd, params, timeout=30, client_id=None):
            calls.append(cmd)
            if len(calls) == 1:
                return first_result, None
            return retry_result, None

        monkeypatch.setattr(bfetch, 'send_browser_command', fake_send)
        monkeypatch.setattr(bfetch, 'is_extension_connected', lambda *a, **k: True)
        monkeypatch.setattr(bfetch, '_get_active_client', lambda: None)
        engaged = []
        monkeypatch.setattr('lib.browser.cookie_capture.handle_login_wall',
                            lambda url, final_url='': engaged.append(url) or captured)
        return bfetch, calls, engaged

    def test_walled_result_returns_none_and_engages_capture(self, monkeypatch):
        bfetch, calls, engaged = self._prime(monkeypatch, {
            'url': 'https://ssosv.internal.example.com/sson/login?client_id=x',
            'title': '统一登录中心',
            'text': '二维码登录 简体中文 登录您的账号 ' * 20,
        })
        out = bfetch.fetch_url_via_browser(
            'https://api.openai.com/ml/modelPlaza/modelInfo')
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
            'https://api.openai.com/ml/modelPlaza/modelInfo')
        assert calls == ['fetch_url', 'fetch_url'], 'one inline retry after capture'
        assert out is not None and 'real model list' in out

    def test_non_wall_result_untouched(self, monkeypatch):
        bfetch, calls, engaged = self._prime(monkeypatch, {
            'url': 'https://example.com/article',
            'title': 'A normal page',
            'text': 'perfectly fine content ' * 50,
        })
        out = bfetch.fetch_url_via_browser('https://example.com/article')
        assert out is not None
        assert engaged == []

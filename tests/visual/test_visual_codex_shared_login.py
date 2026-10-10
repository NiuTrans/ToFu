"""Real browser: borrowed Codex login completes without a popup or device code."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect
from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize('button', ['oauthCodexLoginBtn', 'oauthCodexDeviceBtn', 'oauthCodexReuseBtn'])
def test_shared_login_completes_immediately(page, screenshot_dir, button):
    connected = False
    calls = []
    success = {'status': 'success', 'authenticated': True, 'provider_ready': True,
               'provider': 'codex', 'credential_source': 'local_codex',
               'email': 'shared@example.test'}

    def status(route):
        route.fulfill(json={'codex': success if connected else {'authenticated': False, 'shared_login_reuse_supported': True},
                            'claude': {'authenticated': False, 'shared_login_reuse_supported': True}})

    def login(route):
        if button == 'oauthCodexReuseBtn':
            assert route.request.post_data_json == {'provider': 'codex', 'reuse_only': True}
        nonlocal connected
        connected = True
        calls.append(route.request.url)
        route.fulfill(json=success)

    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'), status)
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/(?:login|device-login)(?:\?.*)?$'), login)
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'oauth')
    if button == 'oauthCodexReuseBtn':
        page.locator('#oauthCodexSharedLogin').scroll_into_view_if_needed()
        page.screenshot(path=str(Path(screenshot_dir) / 'codex-reuse-ready.png'))
    before = len(page.context.pages)
    page.locator('#' + button).click()
    expect(page.locator('#oauthCodexStatus')).to_have_class(re.compile(r'\bauthenticated\b'))
    expect(page.locator('#oauthCodexDevice')).to_be_hidden()
    expect(page.locator('#oauthCodexManual')).to_be_hidden()
    assert len(calls) == 1
    assert len(page.context.pages) == before
    page.screenshot(path=str(Path(screenshot_dir) / (button + '-shared.png')))


@pytest.mark.parametrize('width', [390, 1280])
def test_explicit_reuse_unavailable_stays_inline(page, screenshot_dir, width):
    page.set_viewport_size({'width': width, 'height': 900})
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': False, 'shared_login_reuse_supported': True},
                                                  'claude': {'authenticated': False, 'shared_login_reuse_supported': True}}))
    calls = []
    def unavailable(route):
        calls.append(route.request.url)
        route.fulfill(json={'provider': 'codex', 'status': 'unavailable'})
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/login(?:\?.*)?$'), unavailable)
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'oauth')
    page.locator('#oauthCodexReuseBtn').click()
    expect(page.locator('#oauthCodexSharedLogin')).to_have_attribute('data-state', 'error')
    expect(page.locator('#oauthCodexSharedFeedback')).to_be_visible()
    expect(page.locator('#oauthCodexReuseBtn')).to_be_enabled()
    expect(page.locator('#oauthCodexManual')).to_be_hidden()
    assert len(calls) == 1
    assert len(page.context.pages) == 1
    assert page.locator('#oauthCodexSharedLogin').evaluate('(el) => el.scrollWidth <= el.clientWidth')
    page.screenshot(path=str(Path(screenshot_dir) / f'codex-reuse-unavailable-{width}.png'))


@pytest.mark.parametrize('source', ['local_codex', 'desktop_codex', 'tofu'])
def test_reuse_shows_source_and_keeps_existing_login(page, screenshot_dir, source):
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': True,
                   'credential_source': source, 'provider_ready': True, 'shared_login_reuse_supported': True},
                   'claude': {'authenticated': False, 'shared_login_reuse_supported': True}}))
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'oauth')
    expect(page.locator('#oauthCodexReuseBtn')).to_be_disabled()
    expected = {'local_codex': 'Tofu 所在设备', 'desktop_codex': '桌面代理', 'tofu': '独立授权'}[source]
    expect(page.locator('#oauthCodexSharedFeedback')).to_contain_text(expected)
    page.locator('#oauthCodexSharedLogin').scroll_into_view_if_needed()
    page.screenshot(path=str(Path(screenshot_dir) / f'codex-reuse-{source}.png'))


def test_reuse_single_flight_and_network_failure(page):
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': False, 'shared_login_reuse_supported': True}}))
    requests = []
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/login(?:\?.*)?$'),
               lambda route: requests.append(route))
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'oauth')
    page.locator('#oauthCodexReuseBtn').evaluate('(button) => { button.click(); button.click(); }')
    expect(page.locator('#oauthCodexReuseBtn')).to_be_disabled()
    expect(page.locator('#oauthCodexSharedLogin')).to_have_attribute('aria-busy', 'true')
    page.wait_for_timeout(100)
    assert len(requests) == 1
    requests[0].abort()
    expect(page.locator('#oauthCodexSharedLogin')).to_have_attribute('data-state', 'error')
    expect(page.locator('#oauthCodexReuseBtn')).to_be_enabled()


def test_old_server_cannot_turn_reuse_into_new_authorization(page):
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': False}}))
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'openSettings')
    _invoke_action(page, 'switchSettingsTab', 'oauth')
    expect(page.locator('#oauthCodexReuseBtn')).to_be_disabled()
    expect(page.locator('#oauthCodexSharedFeedback')).to_contain_text('重启 Tofu')


@pytest.mark.parametrize('kind,width', [('internal', 390), ('oauth_reauth', 1280)])
def test_subscription_error_button_opens_login_settings(page, screenshot_dir, kind, width):
    page.set_viewport_size({'width': width, 'height': 900})
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': False,
                   'shared_login_reuse_supported': True}}))
    logins = []
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/login(?:\?.*)?$'),
               lambda route: (logins.append(route.request.url), route.fulfill(json={})))
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    html = _invoke_action(page, 'renderErrorEnvelope', {
        'kind': kind, 'message': 'Internal error',
        'detail': 'local request preparation failed: Codex subscription not logged in (no valid OAuth token)',
        'titleKey': f'err.k.{kind}.title', 'hintKey': f'err.k.{kind}.hint',
    })
    page.evaluate("""html => {
      const host = document.createElement('div');
      host.id = 'subscriptionErrorFixture';
      host.innerHTML = html;
      document.body.prepend(host);
    }""", html)
    button = page.locator('#subscriptionErrorFixture .error-block-signin-btn')
    expect(button).to_be_visible()
    expect(button).to_have_text('前往订阅登录')
    button.click()
    expect(page.locator('#settingsModal')).to_have_class(re.compile(r'\bopen\b'))
    expect(page.locator('#settingsTab_oauth')).to_have_class(re.compile(r'\bactive\b'))
    expect(page.locator('#oauthCodexLoginBtn')).to_be_visible()
    assert logins == []
    page.screenshot(path=str(Path(screenshot_dir) / f'subscription-error-login-{width}.png'))


@pytest.mark.parametrize('http_status', [200, 400])
def test_reuse_expired_reason_is_actionable(page, http_status):
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': False,
                   'shared_login_reuse_supported': True}}))
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/login(?:\?.*)?$'),
               lambda route: route.fulfill(status=http_status, json={'provider': 'codex',
                   'status': 'error' if http_status == 400 else 'unavailable',
                   'auth_issue': 'codex_access_expired'}))
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'jumpToSetting', 'oauth')
    page.locator('#oauthCodexReuseBtn').click()
    expect(page.locator('#oauthCodexSharedFeedback')).to_contain_text('已过期')
    expect(page.locator('#oauthCodexSharedFeedback')).to_contain_text('Codex')
    expect(page.locator('#oauthCodexSharedFeedback')).not_to_contain_text('CODEX_HOME')


@pytest.mark.parametrize('proxy_status', [404, 405])
def test_reuse_through_post_rejecting_proxy(page, proxy_status):
    page.route(re.compile(r'.*/api/(?:v4/)?oauth/status(?:\?.*)?$'),
               lambda route: route.fulfill(json={'codex': {'authenticated': False,
                   'shared_login_reuse_supported': True}}))
    calls = []

    def login(route):
        request = route.request
        calls.append(request.method)
        if request.method == 'POST':
            assert request.post_data_json == {'provider': 'codex', 'reuse_only': True}
            route.fulfill(status=proxy_status, json={'error': 'Proxy rejected POST'})
        else:
            from urllib.parse import parse_qs, urlparse
            assert parse_qs(urlparse(request.url).query) == {
                'provider': ['codex'], 'reuse_only': ['true']}
            route.fulfill(json={'status': 'success', 'authenticated': True,
                'provider_ready': True, 'credential_source': 'local_codex'})

    page.route(re.compile(r'.*/api/(?:v4/)?oauth/login(?:\?.*)?$'), login)
    _wait_ready(page)
    page.add_locator_handler(page.locator('#obCloseX'), lambda locator: locator.click())
    _invoke_action(page, 'jumpToSetting', 'oauth')
    before = len(page.context.pages)
    page.locator('#oauthCodexReuseBtn').click()
    expect(page.locator('#oauthCodexStatus')).to_have_class(re.compile(r'\bauthenticated\b'))
    expect(page.locator('#oauthCodexSharedLogin')).to_have_attribute('data-state', 'connected')
    assert calls == ['POST', 'GET']
    assert len(page.context.pages) == before

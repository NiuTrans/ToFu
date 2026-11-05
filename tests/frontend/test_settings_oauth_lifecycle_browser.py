"""Real Settings controls retire login feedback and resume device observation."""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['login_prepare_retired', 'reuse_settles_retired', 'device_reopen'])
def test_settings_oauth_opening_lifetime(browser, live_server, scenario):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    context.add_init_script("""localStorage.setItem('tofu_ui_lang','en');
        localStorage.setItem('tofu_onboarding_v1_done','1');""")
    page = context.new_page()
    errors, pending, popups = [], [], []
    state = {'reads': 0, 'device': False}
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('popup', lambda popup: popups.append(popup))

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/oauth/status':
            state['reads'] += 1
            codex = {'status': 'not_started', 'authenticated': False,
                     'shared_login_reuse_supported': True}
            if state['device']:
                codex = {'status': 'waiting_callback', 'authenticated': False,
                         'device': {'user_code': 'SYNTH-CODE',
                                    'verification_url': 'https://example.invalid/device'}}
                if state['reads'] >= 3:
                    codex = {'status': 'success', 'authenticated': True,
                             'provider_ready': True, 'model_count': 1}
            return route.fulfill(json={'data': {'claude': {'status': 'not_started'}, 'codex': codex}, 'meta': {}})
        if parsed.path == '/api/oauth/login':
            pending.append(route)
            return None
        if parsed.path == '/api/v4/oauth/device-login':
            state['device'] = True
            return route.fulfill(json={'data': {'status': 'started', 'user_code': 'SYNTH-CODE',
                'verification_url': 'https://example.invalid/device'}, 'meta': {}})
        return route.continue_()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        opening = page.locator('[data-tofu-action="openSettings()"]').first
        opening.click()
        modal = page.locator('#settingsModal')
        expect(modal).to_be_visible(timeout=20000)
        page.locator('.settings-tab[data-tab="oauth"]').click()
        login = page.locator('#oauthCodexLoginBtn')
        expect(login).to_be_enabled()
        if scenario == 'device_reopen':
            page.locator('#oauthCodexDeviceBtn').click()
            expect(page.locator('#oauthCodexDeviceCode')).to_have_text('SYNTH-CODE')
        else:
            button = page.locator('#oauthCodexReuseBtn') if scenario == 'reuse_settles_retired' else login
            with page.expect_request(lambda request: urlparse(request.url).path == '/api/oauth/login'):
                button.click()
            assert len(pending) == 1
        modal.locator('.settings-close-btn').click()
        expect(modal).not_to_be_visible()
        opening.click()
        expect(modal).to_be_visible()
        page.locator('.settings-tab[data-tab="oauth"]').click()
        if scenario == 'device_reopen':
            expect(page.locator('#oauthCodexStatus')).to_have_class('oauth-status-badge authenticated', timeout=10000)
            assert state['reads'] >= 3
        else:
            hint = page.locator('#settingsStatusHint').inner_text()
            result = {'status': 'success'} if scenario == 'reuse_settles_retired' else {
                'auth_url': 'https://example.invalid/oauth', 'exchange': {'state': 'synthetic-state'}}
            with page.expect_response(lambda response: urlparse(response.url).path == '/api/oauth/login'):
                pending.pop().fulfill(json=result)
            if scenario == 'reuse_settles_retired':
                expect(page.locator('#oauthCodexReuseBtn')).to_be_enabled()
                expect(page.locator('#oauthCodexSharedLogin')).to_have_attribute('data-state', 'idle')
            else:
                # Let the fulfilled body and popup side effects reach the renderer.
                page.wait_for_timeout(150)
            expect(page.locator('#settingsStatusHint')).to_have_text(hint)
            expect(page.locator('#oauthCodexStatus')).not_to_have_class('oauth-status-badge authenticated')
            assert not popups
        assert not errors
    finally:
        for route in pending:
            route.abort()
        context.close()

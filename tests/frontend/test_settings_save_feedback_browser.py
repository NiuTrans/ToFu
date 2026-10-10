"""A real Settings save waits for feature acknowledgement and allows retry."""
from urllib.parse import urlparse
import pytest

pytestmark = pytest.mark.visual


def test_settings_feature_save_waits_and_keeps_failure_visible(browser, live_server):
    from playwright.sync_api import expect
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');")
    page = context.new_page()
    pending = []
    writes = []
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/features' and route.request.method == 'POST':
            writes.append(route.request.post_data_json)
            if len(writes) == 1:
                pending.append(route)
                return None
            return route.fulfill(json={'data': {}, 'meta': {}})
        if parsed.path == '/api/v4/server-config' and route.request.method == 'POST':
            return route.fulfill(json={'data': {}, 'meta': {}})
        return route.continue_()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        modal = page.locator('#settingsModal')
        expect(modal).to_be_visible(timeout=20000)
        page.locator('.settings-tab[data-tab="general"]').click()
        page.locator('details.settings-disclosure').filter(
            has=page.locator('#settingTradingEnabled')).locator('summary').click()
        toggle = page.locator('#settingTradingEnabled')
        track = page.locator("label.stg-toggle").filter(has=toggle)
        expect(track).to_be_visible()
        previous = toggle.is_checked()
        track.click()
        expect(toggle).to_be_checked(checked=not previous)
        save = page.locator('#settingsSaveBtn')
        with page.expect_request(lambda request: urlparse(request.url).path == '/api/v4/features'
                                 and request.method == 'POST'):
            save.click()
        expect(save).to_be_disabled()
        expect(modal).to_be_visible()
        assert len(pending) == 1
        pending.pop().fulfill(status=403, json={
            'type': 'about:blank', 'title': 'Denied', 'status': 403,
            'code': 'permission_denied', 'detail': 'fixture write refused'},
            content_type='application/problem+json')
        expect(save).to_be_enabled()
        expect(page.locator('#settingsStatusHint')).to_contain_text('Could not save')
        expect(modal).to_be_visible()
        save.click()
        expect(modal).not_to_be_visible(timeout=20000)
        assert len(writes) == 2 and writes[0] == writes[1]
        assert not errors, errors
    finally:
        for route in pending:
            route.abort()
        context.close()

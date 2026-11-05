"""A real Settings save waits for feature acknowledgement and allows retry."""
from urllib.parse import urlparse
import pytest

pytestmark = pytest.mark.visual


def test_settings_feature_save_waits_and_keeps_failure_visible(browser, live_server):
    from playwright.sync_api import expect
    context = browser.new_context(service_workers='block')
    context.add_init_script("""localStorage.setItem('tofu_ui_lang','en');
        localStorage.setItem('tofu_onboarding_v1_done', '1');""")
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
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        modal = page.locator('#settingsModal')
        expect(modal).to_be_visible(timeout=20000)
        page.locator('.settings-tab[data-tab="general"]').click()
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


@pytest.mark.parametrize('reopen', [False, True], ids=['later_edit', 'new_opening'])
def test_settings_save_keeps_captured_input_and_current_view(browser, live_server, reopen):
    from playwright.sync_api import expect
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    pending, writes, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/features' and route.request.method == 'POST':
            pending.append(route)
            return
        if parsed.path == '/api/v4/server-config' and route.request.method == 'POST':
            writes.append(route.request.post_data_json)
            return route.fulfill(json={'data': {}, 'meta': {}})
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    def show_search():
        page.locator('.settings-tab[data-tab="search"]').click()
        details = page.locator('details.settings-disclosure').filter(
            has=page.locator('#settingSkipDomains'))
        if not details.evaluate('(element) => element.open'):
            details.locator('summary').click()

    def add_blocked(domain):
        chip_field = page.locator('#settingSkipDomains .chip-add')
        chip_field.fill(domain)
        chip_field.press('Enter')

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="general"]').click()
        page.locator('label.stg-toggle').filter(has=page.locator('#settingTradingEnabled')).click()
        show_search()
        add_blocked('original.example')
        with page.expect_request(lambda request: request.method == 'POST'
                and urlparse(request.url).path == '/api/v4/features'):
            page.locator('#settingsSaveBtn').click()
        assert len(pending) == 1 and not writes
        if reopen:
            page.locator('#settingsModal .settings-close-btn').click()
            page.locator('[data-tofu-action="openSettings()"]').first.click()
            show_search()
        add_blocked('newer.example')
        with page.expect_request(lambda request: request.method == 'POST'
                and urlparse(request.url).path == '/api/v4/server-config'):
            pending.pop().fulfill(json={'data': {}, 'meta': {}})
        expect(page.locator('#settingsSaveBtn')).to_be_enabled()
        expect(page.locator('#settingsModal')).to_be_visible()
        expect(page.locator('#settingSkipDomains .chip-text', has_text='newer.example')).to_be_visible()
        assert len(writes) == 1
        saved = writes[0]['search']['skip_domains']
        assert 'original.example' in saved and 'newer.example' not in saved
        assert not errors, errors
    finally:
        for route in pending:
            route.abort()
        context.close()


def test_settings_loading_time_edits_join_the_waiting_save(browser, live_server):
    from playwright.sync_api import expect
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    pending, writes, errors = [], [], []
    state = {'hold': False}
    page.on('pageerror', lambda error: errors.append(str(error)))

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/server-config' and route.request.method == 'GET' and state['hold']:
            pending.append(route)
            return
        if parsed.path == '/api/v4/features' and route.request.method == 'POST':
            writes.append(route.request.post_data_json)
            return route.fulfill(json={'data': {}, 'meta': {}})
        if parsed.path == '/api/v4/server-config' and route.request.method == 'POST':
            return route.fulfill(json={'data': {}, 'meta': {}})
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        state['hold'] = True
        with page.expect_request(lambda request: request.method == 'GET'
                and urlparse(request.url).path == '/api/v4/server-config'):
            page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="general"]').click()
        page.locator('#settingsSaveBtn').click()
        expect(page.locator('#settingsSaveBtn')).to_be_disabled()
        toggle = page.locator('#settingTradingEnabled')
        wanted = not toggle.is_checked()
        page.locator('label.stg-toggle').filter(has=toggle).click()
        expect(toggle).to_be_checked(checked=wanted)
        assert pending and not writes
        state['hold'] = False
        for route in pending:
            route.fulfill(json={'ok': True, 'presets': {}})
        pending.clear()
        expect(page.locator('#settingsModal')).not_to_be_visible(timeout=20000)
        assert writes == [{'trading_enabled': wanted}]
        assert not errors, errors
    finally:
        for route in pending:
            route.abort()
        context.close()

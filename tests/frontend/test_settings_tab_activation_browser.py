"""Actual Settings navigation owns independent loads and preserves open edits."""
from collections import Counter
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['lazy_visits', 'retired_catalog', 'preserve_context_edit'])
def test_settings_independent_tab_lifetime(browser, live_server, scenario):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    context.add_init_script("""localStorage.setItem('tofu_ui_lang','en');
        localStorage.setItem('tofu_onboarding_v1_done','1');""")
    page = context.new_page()
    calls, pending, errors = Counter(), [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    catalog_path = '/api/v4/mcp/catalog'
    independent = [catalog_path, '/api/v4/mcp/updates', '/api/v4/skills',
                   '/api/v4/tools', '/api/v4/context', '/api/v4/database/status']

    def catalog(name):
        return {'data': {'catalog': [{'id': 'synthetic', 'name': name,
                'description': 'Fixture server', 'category': 'Other',
                'installed': True, 'connected': True, 'tools_count': 1}]}, 'meta': {}}

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        calls[parsed.path] += 1
        if parsed.path == catalog_path:
            if scenario == 'retired_catalog' and calls[catalog_path] == 1:
                pending.append(route)
                return
            return route.fulfill(json=catalog('Current server'))
        if parsed.path == '/api/v4/mcp/updates':
            return route.fulfill(json={'data': {'updates': {}}, 'meta': {}})
        if parsed.path == '/api/v4/context' and route.request.method == 'GET':
            return route.fulfill(json={'data': {'items': [
                {'id': 'synthetic', 'type': 'identity', 'text': 'Saved context'}]}, 'meta': {}})
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    def tab(name):
        page.locator(f'.settings-tab[data-tab="{name}"]').click()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        opening = page.locator('[data-tofu-action="openSettings()"]').first
        before = calls.copy()
        opening.click()
        modal = page.locator('#settingsModal')
        expect(modal).to_be_visible(timeout=20000)
        expect(page.locator('#settingsSaveBtn')).to_be_enabled()
        assert {path: calls[path] - before[path] for path in independent} == dict.fromkeys(independent, 0)
        if scenario == 'preserve_context_edit':
            tab('preferences')
            row = page.locator('.ctx-item').filter(has_text='Saved context')
            row.locator('button[data-context-action="edit"]').click()
            editor = page.locator('[data-context-field="text"]')
            editor.fill('Unsaved context')
            tab('general')
            tab('preferences')
            expect(editor).to_have_value('Unsaved context')
            assert calls['/api/v4/context'] - before['/api/v4/context'] == 1
        else:
            with page.expect_request(lambda request: urlparse(request.url).path == catalog_path):
                tab('mcp')
            if scenario == 'retired_catalog':
                assert len(pending) == 1
                modal.locator('.settings-close-btn').click()
                opening.click()
                expect(modal).to_be_visible()
                tab('mcp')
                expect(page.locator('#mcpCatalogGrid')).to_contain_text('Current server')
                pending.pop().fulfill(json=catalog('Retired server'))
                page.wait_for_timeout(100)
                expect(page.locator('#mcpCatalogGrid')).not_to_contain_text('Retired server')
                assert calls[catalog_path] - before[catalog_path] == 2
            else:
                expect(page.locator('#mcpCatalogGrid')).to_contain_text('Current server')
                tab('general')
                tab('mcp')
                expect(page.locator('#mcpCatalogGrid')).to_contain_text('Current server')
                assert calls[catalog_path] - before[catalog_path] == 1
                assert calls['/api/v4/mcp/updates'] - before['/api/v4/mcp/updates'] == 1
        assert not errors, errors
    finally:
        for route in pending:
            route.abort()
        context.close()

"""Actual MCP controls preserve dialogs and ordered tool edits across responses."""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['install_reopen', 'custom_connect_failure', 'ordered_tool_failure'])
def test_mcp_command_controls(browser, live_server, scenario):
    from playwright.sync_api import expect

    context = browser.new_context(service_workers='block')
    context.add_init_script("""localStorage.setItem('tofu_ui_lang','en');
        localStorage.setItem('tofu_onboarding_v1_done','1');""")
    page = context.new_page()
    errors, installs, writes, pending, creates = [], [], [], [], []
    state = {'catalog_reads': 0, 'connected_alpha': scenario == 'ordered_tool_failure'}
    page.on('pageerror', lambda error: errors.append(str(error)))

    def data(route, body):
        return route.fulfill(json={'data': body, 'meta': {}})

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/mcp/catalog':
            state['catalog_reads'] += 1
            catalog = [{'id': name, 'name': 'Synthetic ' + name, 'description': 'Fixture server',
                        'category': 'Other', 'installed': name == 'alpha' and state['connected_alpha'],
                        'connected': name == 'alpha' and state['connected_alpha'], 'tools_count': 2,
                        'env_specs': [{'key': 'TOKEN', 'label': 'Synthetic token', 'required': True}]
                        } for name in ('alpha', 'beta')]
            return data(route, {'catalog': catalog})
        if parsed.path == '/api/v4/mcp/updates':
            return data(route, {'updates': {}})
        if parsed.path == '/api/v4/mcp/catalog/install':
            installs.append(route.request.post_data_json)
            pending.append(route)
            return
        if parsed.path == '/api/v4/mcp/servers':
            creates.append(route.request.post_data_json)
            return data(route, {'ok': True})
        if parsed.path == '/api/v4/mcp/connect':
            return data(route, {'ok': False, 'error': 'synthetic connection refused'})
        if parsed.path == '/api/v4/mcp/tools':
            return data(route, {'tools': [{'name': name, 'enabled': True, 'description': name} for name in ('one', 'two')]})
        if parsed.path == '/api/v4/mcp/servers/alpha/tools':
            writes.append(route.request.post_data_json)
            if len(writes) == 2:
                return route.fulfill(status=403, json={'type': 'about:blank', 'title': 'Denied', 'status': 403,
                    'code': 'permission_denied', 'detail': 'synthetic selection refusal'}, content_type='application/problem+json')
            pending.append(route)
            return
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    def install_dialog(server):
        page.locator(f'''[data-tofu-action="_mcpOpenInstallModal('{server}')"]''').click()
        expect(page.locator('#mcpInstallOverlay')).to_be_visible()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        expect(page.locator('#settingsModal')).to_be_visible(timeout=20000)
        page.locator('.settings-tab[data-tab="mcp"]').click()
        expect(page.locator('#mcpCatalogGrid')).to_contain_text('Synthetic alpha')
        if scenario == 'install_reopen':
            install_dialog('alpha')
            token = page.locator('#mcpInstallFields [data-key="TOKEN"]')
            token.fill('synthetic-original')
            with page.expect_request(lambda request: urlparse(request.url).path == '/api/v4/mcp/catalog/install'):
                page.locator('#mcpInstallBtn').click()
            page.locator('#mcpInstallOverlay [data-tofu-action="_mcpCloseInstallModal()"]').click()
            install_dialog('beta')
            token.fill('synthetic-later')
            before = state['catalog_reads']
            state['connected_alpha'] = True
            with page.expect_request(lambda request: urlparse(request.url).path == '/api/v4/mcp/catalog'):
                data(pending.pop(), {'ok': True, 'status': 'ready', 'tools_count': 2})
            # Exercise the existing delayed success-close boundary in the actual browser.
            page.wait_for_timeout(1400)
            expect(page.locator('#mcpInstallOverlay')).to_be_visible()
            expect(page.locator('#mcpInstallTitle')).to_have_text('Synthetic beta')
            expect(token).to_have_value('synthetic-later')
            assert state['catalog_reads'] == before + 1
            assert installs == [{'id': 'alpha', 'env': {'TOKEN': 'synthetic-original'}}]
        elif scenario == 'custom_connect_failure':
            page.locator('#mcpAddCustomBtn').click()
            page.locator('#mcpNewName').fill('synthetic-custom')
            page.locator('#mcpNewCommand').fill('synthetic-command')
            page.locator('#mcpAddSaveBtn').click()
            expect(page.locator('#mcpAddStatus')).to_contain_text('synthetic connection refused')
            expect(page.locator('#mcpAddStatus')).to_have_class('mcp-install-status error')
            expect(page.locator('#mcpAddSaveBtn')).to_be_enabled()
            expect(page.locator('#mcpAddOverlay')).to_be_visible()
            assert len(creates) == 1 and creates[0]['name'] == 'synthetic-custom'
        else:
            page.locator('''[data-tofu-action="_mcpToggleToolsPanel('alpha')"]''').click()
            one = page.locator('''#mcpCatalogGrid input[data-tofu-action-change*="'one'"]''')
            two = page.locator('''#mcpCatalogGrid input[data-tofu-action-change*="'two'"]''')
            one.locator('..').click()
            two.locator('..').click()
            expect(two).not_to_be_checked()
            assert writes == [{'disabled_tools': ['one']}]
            with page.expect_response(lambda response: response.status == 403
                    and urlparse(response.url).path == '/api/v4/mcp/servers/alpha/tools'):
                data(pending.pop(0), {'mcp_tool_summary': {'servers': [{'name': 'alpha', 'count': 1}], 'total': 1}})
            assert writes == [{'disabled_tools': ['one']}, {'disabled_tools': ['one', 'two']}]
            expect(one).not_to_be_checked()
            expect(two).to_be_checked()
        assert not errors, errors
    finally:
        for route in pending:
            route.abort()
        context.close()

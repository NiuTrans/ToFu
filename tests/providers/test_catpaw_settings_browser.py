"""Real Catpaw controls keep explicit disconnect and retired-view intent."""
import copy
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['disconnect_reconnect', 'initial_auto', 'reopen_import', 'cancel', 'retired_confirmation', 'provider_delete_during_read'])
def test_catpaw_settings_lifecycle(browser, live_server, scenario):
    from playwright.sync_api import expect
    from lib.model_routing import normalize_document
    from lib.model_routing.controls import reasoning_controls
    from tests.model_routing.test_model_routing_contract import _full_document
    from tests.support.model_registration import complete_registration

    document = normalize_document(complete_registration(_full_document()))
    document['providers'][0]['brand'] = 'catpaw'
    provider_id = document['providers'][0]['provider_id']
    original = copy.deepcopy(document)
    state = {'document': document, 'revision': document['revision'], 'imports': 0,
             'deletes': 0, 'available': True, 'pending': None}
    status_reads = []
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def connected():
        return any(row.get('brand') == 'catpaw' for row in state['document']['providers'])

    def remove_provider():
        current = state['document']
        accesses = {row['provider_access_id'] for row in current['provider_accesses']
                    if row['provider_id'] == provider_id}
        offerings = {row['offering_id'] for row in current['offerings']
                     if row['provider_access_id'] in accesses}
        current['providers'] = [row for row in current['providers'] if row['provider_id'] != provider_id]
        for field in ('provider_accesses', 'connections', 'credentials', 'offerings'):
            current[field] = [row for row in current[field] if row['provider_access_id'] not in accesses]
        current['deployments'] = [row for row in current['deployments'] if row['offering_id'] not in offerings]

    def complete_import(route):
        state['imports'] += 1
        state['revision'] += 1
        state['document'] = copy.deepcopy(original)
        state['document']['revision'] = state['revision']
        route.fulfill(json={'ok': True, 'provider_id': provider_id, 'connected': True,
                            'created': True, 'rotated': False, 'model_count': 2, 'revision': state['revision']})

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/providers/catpaw/local-import':
            if route.request.method == 'GET':
                if state.get('hold_status'):
                    status_reads.append(route)
                    return
                return route.fulfill(json={'ok': True, 'available': state['available'],
                    'connected': connected(), 'installed': True, 'model_count': 2, 'revision': state['revision']})
            if state.get('hold_import'):
                state['pending'] = route
                return
            return complete_import(route)
        if parsed.path == '/api/v4/providers/catpaw/local-import/login':
            raise AssertionError('No real CLI login may escape this fixture')
        if parsed.path == f'/api/v4/providers/{provider_id}' and route.request.method == 'DELETE':
            assert route.request.post_data_json['expected_revision'] == state['revision']
            state['deletes'] += 1
            state['revision'] += 1
            remove_provider()
            state['document']['revision'] = state['revision']
            return route.fulfill(json={'ok': True, 'deleted': provider_id, 'revision': state['revision']})
        if parsed.path == '/api/v4/model-routing':
            assert route.request.method == 'GET', 'Catpaw must not replace a whole routing document'
            return route.fulfill(json={'ok': True, 'revision': state['revision'],
                'model_routing': state['document'], 'metadata_inheritance_supported': True,
                'reasoning_controls': reasoning_controls(state['document'])})
        if parsed.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    def open_catpaw():
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="oauth"]').click()

    context.route('**/*', handle)
    try:
        state['hold_status'] = scenario == 'provider_delete_during_read'
        if scenario in {'initial_auto', 'reopen_import'}:
            remove_provider()
            state['available'] = scenario == 'initial_auto'
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        open_catpaw()
        connect = page.locator('#oauthCatpawImportBtn')
        disconnect = page.locator('#oauthCatpawDisconnectBtn')
        close = page.locator('#settingsModal .settings-close-btn')
        if scenario == 'provider_delete_during_read':
            page.locator('.settings-tab[data-tab="api"]').click()
            provider = page.locator(f'.stg-provider-card-v2[data-provider-id="{provider_id}"]')
            if not provider.evaluate('(element) => element.open'):
                provider.locator('> summary').click()
            provider.locator('[data-tofu-action*="_deleteModelRoutingProvider"]').click()
            page.locator('.app-dialog-overlay .app-dialog-ok').click()
            expect(provider).to_have_count(0)
            assert status_reads, 'exercise deletion before the initial Catpaw status reply'
            state['hold_status'] = False
            for route in status_reads:
                route.fulfill(json={'ok': True, 'available': True, 'connected': False,
                                    'installed': True, 'model_count': 0, 'revision': state['revision']})
            status_reads.clear()
            page.locator('.settings-tab[data-tab="oauth"]').click()
            expect(connect).to_be_visible()
            expect(connect).to_be_enabled()
            assert state['deletes'] == 1
            assert state['imports'] == 0
        elif scenario == 'reopen_import':
            expect(connect).to_be_enabled()
            state.update(available=True, hold_import=True)
            with page.expect_request(lambda request: request.method == 'POST'
                                     and urlparse(request.url).path == '/api/v4/providers/catpaw/local-import'):
                connect.click()
            expect(connect).to_be_disabled()
            close.click()
            open_catpaw()
            expect(connect).to_be_disabled()
            route = state['pending']
            assert route is not None
            state['pending'] = None
            complete_import(route)
            expect(disconnect).to_be_visible()
            expect(disconnect).to_be_enabled()
            assert state['imports'] == 1
        else:
            expect(disconnect).to_be_visible()
            expect(disconnect).to_be_enabled()
            if scenario == 'initial_auto':
                close.click()
                open_catpaw()
                expect(disconnect).to_be_enabled()
                assert state['imports'] == 1
            else:
                disconnect.click()
                dialog = page.locator('.app-dialog-overlay')
                expect(dialog).to_be_visible()
                if scenario == 'retired_confirmation':
                    # Dispatch the panel's real close action while the separately
                    # owned confirmation is pending (host-driven teardown).
                    close.evaluate('(button) => button.click()')
                    dialog.locator('.app-dialog-ok').click()
                    open_catpaw()
                    expect(disconnect).to_be_enabled()
                    assert state['deletes'] == 0
                elif scenario == 'cancel':
                    dialog.locator('.app-dialog-cancel').click()
                    expect(disconnect).to_be_enabled()
                    assert state['deletes'] == 0
                else:
                    dialog.locator('.app-dialog-ok').click()
                    expect(connect).to_be_visible()
                    expect(connect).to_be_enabled()
                    close.click()
                    open_catpaw()
                    expect(connect).to_be_enabled()
                    assert state['deletes'] == 1
                    assert state['imports'] == 0
                    connect.click()
                    expect(disconnect).to_be_visible()
                    expect(disconnect).to_be_enabled()
                    assert state['imports'] == 1
        assert not errors, errors
    finally:
        for route in status_reads:
            route.abort()
        if state['pending'] is not None:
            state['pending'].abort()
        context.close()

"""Real speech controls keep save intent and plaintext tied to their view."""
from copy import deepcopy
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['unchanged', 'late_key', 'late_metadata', 'reopen', 'initial_loading', 'initial_reopen', 'activation'])
def test_speech_save_lifetime(browser, live_server, scenario):
    from playwright.sync_api import expect
    from lib.model_routing import normalize_document
    from lib.model_routing.controls import reasoning_controls
    from tests.model_routing.test_model_routing_contract import _full_document
    from tests.support.model_registration import complete_registration

    document = normalize_document(complete_registration(_full_document()))
    state = {'document': document, 'revision': document['revision'], 'writes': [], 'whole': []}
    config_reads = []
    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_onboarding_v1_done','1');")
    if scenario == 'activation':
        context.add_init_script('''
          navigator.mediaDevices.getUserMedia = async () => ({getTracks: () => [{stop() {}}]});
          window.MediaRecorder = class {
            state = 'inactive'; mimeType = 'audio/webm';
            start() {this.state = 'recording';}
            stop() {
              this.state = 'inactive';
              this.ondataavailable?.({data: new Blob(['synthetic clip'], {type:'audio/webm'})});
              this.onstop?.();
            }
          };
        ''')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path == '/api/v4/audio/capabilities':
            available = any(row['provider_id'] == 'stt' and row['enabled']
                for row in state['document']['provider_accesses'])
            return route.fulfill(json={'available': available,
                'models': [{'model': 'fixture-stt', 'mode': 'endpoint'}] if available else []})
        if parsed.path == '/api/v4/audio/transcribe':
            assert scenario == 'activation'
            return route.fulfill(json={'ok': True, 'text': 'synthetic dictated draft'})
        if parsed.path.startswith('/api/v4/providers/catpaw/local-import'):
            if parsed.path == '/api/v4/providers/catpaw/local-import' and route.request.method == 'GET':
                return route.fulfill(json={'ok': True, 'available': False, 'connected': False})
            raise AssertionError('Speech settings cannot start a local credential import')
        if parsed.path == '/api/v4/providers/stt' and route.request.method == 'DELETE':
            raise AssertionError('Speech activation must retain its Provider and credentials')
        if parsed.path == '/api/v4/server-config' and route.request.method == 'GET' and state.get('hold_config'):
            config_reads.append(route)
            return
        if (parsed.path == '/api/v4/providers' and route.request.method == 'POST'
                or parsed.path == '/api/v4/providers/stt' and route.request.method == 'PATCH'):
            body = route.request.post_data_json
            assert body['expected_revision'] == state['revision']
            state['writes'].append(body)
            state['pending'] = route
            return
        if parsed.path == '/api/v4/model-routing':
            if route.request.method == 'PUT':
                body = route.request.post_data_json
                assert body['expected_revision'] == state['revision']
                state['revision'] += 1
                state['document'] = normalize_document(body['model_routing'])
                state['document']['revision'] = state['revision']
                state['whole'].append(body)
            return route.fulfill(json={'ok': True, 'revision': state['revision'],
                'model_routing': state['document'], 'metadata_inheritance_supported': True,
                'reasoning_controls': reasoning_controls(state['document'])})
        if parsed.path == '/api/v4/server-config' and route.request.method == 'POST':
            return route.fulfill(json={'ok': True})
        if parsed.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    def acknowledge():
        body = state['writes'][-1]
        bundle = {key: body[key] for key in ('provider', 'provider_access', 'connections',
            'credentials', 'offerings', 'deployments')}
        state['revision'] += 1
        for row in bundle['credentials']:
            if row['credential_id'] in body.get('credential_secrets', {}):
                row.update(secret_reference=f"synthetic-reference-{state['revision']}", key_hint='synt****tail')
        rows_by_field = {**bundle, 'providers': [bundle['provider']],
            'provider_accesses': [bundle['provider_access']],
            'creators': body.get('creators', []), 'models': body.get('models', [])}
        for field, identifier in [('providers', 'provider_id'), ('provider_accesses', 'provider_access_id'),
                ('connections', 'connection_id'), ('credentials', 'credential_id'),
                ('offerings', 'offering_id'), ('deployments', 'deployment_id'),
                ('creators', 'creator_id'), ('models', 'model_id')]:
            rows = rows_by_field[field]
            ids = {row[identifier] for row in rows}
            state['document'][field] = [row for row in state['document'][field]
                if row[identifier] not in ids] + rows
        state['document']['revision'] = state['revision']
        state['document'] = normalize_document(complete_registration(state['document']))
        state.pop('pending').fulfill(json={'ok': True, 'provider': bundle, 'revision': state['revision']})

    def open_speech():
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="speech"]').click()
        if not state.get('hold_config'):
            expect(page.locator('#settingSttEnabled')).to_be_enabled()

    def save_request():
        with page.expect_request(lambda request: request.method in {'POST', 'PATCH'}
                and urlparse(request.url).path in {'/api/v4/providers', '/api/v4/providers/stt'}):
            page.locator('#settingsSaveBtn').click()

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        state['hold_config'] = scenario in {'initial_loading', 'initial_reopen'}
        open_speech()
        if scenario in {'initial_loading', 'initial_reopen'}:
            expect(page.locator('#settingSttEnabled')).to_be_disabled()
            expect(page.locator('#settingsTab_speech')).to_have_attribute('aria-busy', 'true')
            expect(page.locator('#settingSttEnabled')).not_to_be_checked()
            assert config_reads, 'the loading assertion must observe an outstanding config read'
            expect(page.locator('#settingsTab_search')).to_have_attribute('aria-busy', 'true')
            if scenario == 'initial_reopen':
                with page.expect_event('requestfailed', predicate=lambda request:
                        urlparse(request.url).path == '/api/v4/server-config'):
                    page.locator('#settingsModal [data-tofu-action="closeSettings()"]').first.click()
                config_reads.clear()  # The client cancelled this opening's GET.
                expect(page.locator('#settingsModal')).not_to_be_visible()
                open_speech()
                expect(page.locator('#settingSttEnabled')).to_be_disabled()
                expect(page.locator('#settingsTab_search')).to_have_attribute('aria-busy', 'true')
                assert config_reads, 'the new opening owns a fresh config read'
            state['hold_config'] = False
            for route in config_reads:
                route.fulfill(json={'ok': True, 'presets': {}})
            config_reads.clear()
            expect(page.locator('#settingSttEnabled')).to_be_enabled()
            expect(page.locator('#settingsTab_search')).to_have_attribute('aria-busy', 'false')
            assert not page.locator('#settingsTab_search').evaluate('(panel) => panel.hasAttribute("inert")')
        if scenario == 'late_metadata':
            page.locator('.settings-tab[data-tab="api"]').click()
            provider = page.locator('.stg-provider-card-v2[data-provider-id="provider-a"]')
            if not provider.evaluate('(element) => element.open'):
                provider.locator('> summary').click()
            name = provider.locator('input[data-tofu-action-change*="display_name"]')
            name.fill('Renamed before speech save')
            name.press('Tab')
            expect(provider.locator('.stg-provider-name')).to_have_text('Renamed before speech save')
            page.locator('.settings-tab[data-tab="speech"]').click()
        enabled = page.locator('#settingSttEnabled')
        page.locator('label.stg-toggle').filter(has=enabled).click()
        expect(enabled).to_be_checked()
        page.locator('#settingSttModelOpenai').select_option('whisper-1')
        page.locator('#settingSttBaseOpenai').fill('https://fixture.invalid/v1')
        key = page.locator('#settingSttKeyOpenai')
        key.fill('synthetic-first')
        save_request()
        expect(page.locator('#settingsSaveBtn')).to_be_disabled()
        if scenario == 'reopen':
            page.locator('#settingsModal .settings-close-btn').click()
            expect(key).to_have_value('')
            open_speech()
            page.locator('label.stg-toggle').filter(has=enabled).click()
            key.fill('synthetic-reopened')
        elif scenario in {'late_key', 'late_metadata'}:
            key.fill('synthetic-newer')
        acknowledge()
        expect(page.locator('#settingsSaveBtn')).to_be_enabled()
        if scenario == 'activation':
            expect(page.locator('#settingsModal')).not_to_be_visible()
            before = deepcopy(state['document'])
            for active in (False, True):
                if not active:
                    expect(page.locator('#micBtn')).to_be_visible()
                    page.locator('#micBtn').click()
                    expect(page.locator('#micBtn')).to_have_class('mic-btn recording')
                open_speech()
                expect(enabled).to_be_checked(checked=not active)
                expect(key).to_have_value('')
                enabled.locator('..').click()
                expect(enabled).to_be_checked(checked=active)
                with page.expect_request(lambda request: request.method == 'PUT'
                        and urlparse(request.url).path == '/api/v4/model-routing'):
                    page.locator('#settingsSaveBtn').click()
                expect(page.locator('#settingsModal')).not_to_be_visible()
                expected = deepcopy(before)
                expected['revision'] = state['revision']
                next(row for row in expected['provider_accesses']
                     if row['provider_access_id'] == 'stt-access')['enabled'] = active
                assert state['document'] == expected
                assert len(state['writes']) == 1
                if not active:
                    expect(page.locator('#micBtn')).to_be_visible()
                    expect(page.locator('#micBtn')).to_have_class('mic-btn recording')
                    page.locator('#micBtn').click()
                    expect(page.locator('#userInput')).to_have_value('synthetic dictated draft')
                    expect(page.locator('#micBtn')).not_to_be_visible()
                else:
                    expect(page.locator('#micBtn')).to_be_visible()
        elif scenario in {'unchanged', 'initial_loading', 'initial_reopen'}:
            expect(page.locator('#settingsModal')).not_to_be_visible()
            open_speech()
            expect(enabled).to_be_checked()
            expect(key).to_have_value('')
            page.locator('#settingsSaveBtn').click()
            expect(page.locator('#settingsModal')).not_to_be_visible()
            assert len(state['writes']) == 1
        elif scenario == 'reopen':
            expect(page.locator('#settingsModal')).to_be_visible()
            expect(key).to_have_value('synthetic-reopened')
            assert len(state['writes']) == 1
        else:
            expect(page.locator('#settingsModal')).to_be_visible()
            expect(key).to_have_value('synthetic-newer')
            expect(key).to_be_focused()
            save_request()
            assert state['writes'][1]['credential_secrets']['stt-credential'] == 'synthetic-newer'
            acknowledge()
            expect(page.locator('#settingsModal')).not_to_be_visible()
            expect(key).to_have_value('')
        assert len(state['whole']) == (2 if scenario == 'activation' else int(scenario == 'late_metadata'))
        if scenario == 'late_metadata':
            access = next(row for row in state['document']['provider_accesses'] if row['provider_id'] == 'provider-a')
            assert access['display_name'] == 'Renamed before speech save'
        assert not errors, errors
    finally:
        for route in config_reads:
            route.abort()
        if 'pending' in state:
            state.pop('pending').abort()
        context.close()

"""Real Settings page keeps Model identity separate from every wire name."""
import json
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['normal', 'required-selector', 'disabled-face', 'disabled-model-face', 'picker-lifetime', 'credential-lifetime', 'credential-editor', 'credential-replacement', 'credential-replacement-metadata'])
def test_model_header_wire_edit_delete_and_template_add(browser, live_server, tmp_path, scenario):
    from playwright.sync_api import expect
    from tests.model_routing.test_model_routing_contract import _full_document
    from tests.support.model_registration import complete_registration
    from lib.model_routing import normalize_document
    from lib.model_routing.controls import reasoning_controls
    from lib.model_routing.registration import prepare_registration_document
    document = complete_registration(_full_document())
    document['offerings'] = [row for row in document['offerings'] if row['offering_id'] == 'alpha-a']
    document['deployments'] = [row for row in document['deployments'] if row['offering_id'] == 'alpha-a'][:1]
    document['deployments'][0]['wire_model_id'] = 'alpha'
    document = normalize_document(document)

    if scenario.startswith('credential-'):
        document['credentials'][0].update(kind='api_key', secret_reference='synthetic-reference', key_hint='synt****hint')

    face_url = 'https://fixture.example/face'
    if scenario == 'required-selector':
        document['connections'][0]['extra_headers'] = {'x-selector': 'required'}
    if scenario in {'disabled-face', 'disabled-model-face'}:
        document['connections'].append({**document['connections'][0], 'connection_id': 'disabled-face',
            'base_url': face_url, 'gateway_namespace': 'fixture', 'enabled': False})
    state = {'document': document, 'revision': document['revision'], 'writes': [], 'credential_writes': []}
    context = browser.new_context(viewport={'width': 1280, 'height': 1000}, service_workers='block')
    context.add_init_script('localStorage.setItem("claude_client_config", ' + json.dumps(json.dumps({
        'model': 'alpha', 'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'}, 'thinkingDepth': 'default'})) + ');')

    def handle(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc: return route.abort()
        if scenario.startswith('credential-replacement') and url.path.endswith('/secret/reveal'):
            return route.fulfill(json={'ok': True, 'secret': 'synthetic-original'})
        if scenario.startswith('credential-replacement') and url.path.endswith('/secret'):
            assert route.request.method == 'PUT'
            body = route.request.post_data_json
            assert body['expected_revision'] == state['revision']
            state['credential_writes'].append(body)
            state['pending_credential'] = route
            return
        if scenario == 'credential-editor' and url.path == '/api/v4/providers/provider-a' and route.request.method == 'PATCH':
            body = route.request.post_data_json
            assert body['expected_revision'] == state['revision']
            state['credential_writes'].append(body)
            if len(state['credential_writes']) == 1:
                return route.fulfill(status=409, json={'ok': False,
                    'error': {'code': 'model_routing_revision_conflict', 'message': 'synthetic credential refusal'}})
            state['pending_credential'] = route
            return
        if scenario == 'credential-lifetime' and url.path.endswith('/secret/reveal'):
            state['pending_reveal'] = route
            return
        if url.path == '/api/v4/model-routing':
            if route.request.method == 'PUT':
                body = route.request.post_data_json
                assert body['expected_revision'] == state['revision']
                state['document'] = prepare_registration_document(body['model_routing'], previous=state['document'])
                state['revision'] += 1
                state['document']['revision'] = state['revision']
                state['writes'].append(body)
                if scenario == 'picker-lifetime':
                    state['pending_route'] = route
                    return
            return route.fulfill(json={'ok': True, 'revision': state['revision'], 'model_routing': state['document'],
                'metadata_inheritance_supported': True, 'reasoning_controls': reasoning_controls(state['document'])})
        if url.path == '/api/v4/providers/templates':
            return route.fulfill(json={'ok': True, 'items': [{'key': 'test-template', 'name': 'Provider A', 'category': 'relay',
                'base_url': document['connections'][0]['base_url'],
                'faces': {'fixture': {'base_url': face_url if scenario in {'disabled-face', 'disabled-model-face'} else document['connections'][0]['base_url'],
                    'protocol': 'openai', 'extra_headers': {'x-selector': 'required'}}},
                'offering_recipes': [{'model_id': 'alpha', 'request_ids': ['alpha', 'face-alias'],
                    'request_faces': {'alpha': 'fixture', 'face-alias': 'fixture'}},
                    {'model_id': 'beta', 'request_ids': ['beta-provider-wire'],
                     **({'request_faces': {'beta-provider-wire': 'fixture'}} if scenario == 'disabled-model-face' else {})}]}]})
        if url.path == '/api/v4/model-intelligence/aa':
            return route.fulfill(json={'ok': True, 'aa': {'status': 'ok', 'scores': {}}})
        if url.path == '/api/v4/update/check': return route.fulfill(json={'update_available': False})
        if '/chat' in url.path and route.request.method == 'POST': raise AssertionError('Unexpected LLM call')
        return route.continue_()

    context.route('**/*', handle)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if page.locator('#obSkip').is_visible(): page.locator('#obSkip').click()
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.locator('.settings-tab[data-tab="api"]').click()
        provider = page.locator('.stg-provider-card-v2[data-provider-id="provider-a"]')
        if not provider.evaluate('(el) => el.open'): provider.locator('> summary').click()
        expect(provider.locator('.stg-mx-row[data-id="alpha"]')).to_have_count(2)
        wire = provider.locator('.is-alias[data-id="alpha"]')

        if scenario.startswith('credential-replacement'):
            if scenario == 'credential-replacement-metadata':
                name = provider.locator('input[data-tofu-action-change*="display_name"]')
                name.fill('Renamed with credential')
                name.press('Tab')
                expect(provider.locator('.stg-provider-name')).to_have_text('Renamed with credential')
            _credential_replacement_journey(page, provider, state, scenario.endswith('-metadata'))
            assert not errors
            return

        if scenario == 'credential-editor':
            initial_count = provider.locator('.stg-key-card[data-credential-id]').count()
            add = provider.locator('[data-tofu-action*="_startNewV2ApiKey"]')
            add.click()
            pending_input = provider.locator('.stg-key-card--new input')
            pending_input.fill('synthetic-cancelled-key')
            provider.locator('.stg-key-card--new .stg-keys-btn').click()
            expect(pending_input).to_have_count(0)
            assert state['credential_writes'] == []
            add.click()
            pending_input.fill('synthetic-retry-key')
            pending_input.press('Enter')
            expect(page.locator('.app-dialog-overlay')).to_be_visible()
            assert len(state['credential_writes']) == 1, 'A refused Enter submission must not create another POST through blur'
            expect(pending_input).to_be_enabled()
            expect(pending_input).to_have_value('synthetic-retry-key')
            page.locator('.app-dialog-overlay button').click()
            with page.expect_request(lambda request: request.method == 'PATCH' and urlparse(request.url).path == '/api/v4/providers/provider-a'):
                pending_input.press('Enter')
            retired_input = pending_input.element_handle()
            page.locator('#settingsModal .settings-close-btn').click()
            expect(page.locator('#settingsModal')).not_to_be_visible()
            body = state['credential_writes'][-1]
            bundle = {key: body[key] for key in ('provider', 'provider_access', 'connections', 'credentials', 'offerings', 'deployments')}
            for row in bundle['credentials']:
                if row['credential_id'] in body['credential_secrets']:
                    row.update(secret_reference='synthetic-stored-reference', key_hint='synt****tail')
            access_id = bundle['provider_access']['provider_access_id']
            state['document']['credentials'] = [row for row in state['document']['credentials']
                if row['provider_access_id'] != access_id] + bundle['credentials']
            state['revision'] += 1
            state['document']['revision'] = state['revision']
            state.pop('pending_credential').fulfill(json={'ok': True, 'provider': bundle, 'revision': state['revision']})
            expect(page.locator('.toast').filter(has_text='已添加凭证')).to_be_visible()
            assert retired_input.evaluate('(input) => input.value') == ''
            page.locator('[data-tofu-action="openSettings()"]').first.click()
            page.locator('.settings-tab[data-tab="api"]').click()
            expect(provider.locator('.stg-key-card[data-credential-id]')).to_have_count(initial_count + 1)
            assert len(state['credential_writes']) == 2
            assert not state['writes']
            assert not errors
            return

        if scenario == 'credential-lifetime':
            secret = provider.locator('.stg-keys-input').first
            with page.expect_request(lambda r: urlparse(r.url).path.endswith('/secret/reveal')):
                provider.locator('.stg-key-reveal').first.click()
            page.locator('#settingsModal .settings-close-btn').click()
            expect(page.locator('#settingsModal')).not_to_be_visible()
            with page.expect_response(lambda r: urlparse(r.url).path.endswith('/secret/reveal')):
                state.pop('pending_reveal').fulfill(json={'ok': True, 'secret': 'synthetic-secret-value'})
            # Let the actual async action settle before inspecting the hidden
            # panel; a response event alone precedes its Promise callbacks.
            expect(provider.locator('.stg-key-reveal').first).to_be_enabled()
            expect(secret).to_have_value('synt****hint')
            expect(secret).to_have_attribute('readonly', '')
            assert not state['writes']
            assert not errors
            return

        if scenario == 'picker-lifetime':
            provider.locator('[data-tofu-action*="_openProviderModelPicker"]').click()
            page.locator('[data-model-key="creator::beta"]').check()
            with page.expect_request(lambda r: r.method == 'PUT' and urlparse(r.url).path == '/api/v4/model-routing'):
                page.locator('[data-tofu-action="_stageV2CheckedCatalogModels()"]').click()
            page.locator('[data-tofu-action="_closeProviderModelPicker()"]').click()
            other = page.locator('.stg-provider-card-v2[data-provider-id="provider-b"]')
            if not other.evaluate('(el) => el.open'): other.locator('> summary').click()
            other.locator('[data-tofu-action*="_openProviderModelPicker"]').click()
            choice = page.locator('[data-model-key="creator::alpha"]')
            choice.check()
            custom = page.locator('#stgProviderModelCustomId')
            custom.fill('later-custom')
            search = page.locator('.stg-v2-picker-search')
            search.fill('alpha')
            with page.expect_response(lambda r: r.request.method == 'PUT' and urlparse(r.url).path == '/api/v4/model-routing'):
                state.pop('pending_route').fulfill(json={'ok': True, 'revision': state['revision'],
                    'model_routing': state['document'], 'metadata_inheritance_supported': True,
                    'reasoning_controls': reasoning_controls(state['document'])})
            expect(page.locator('.toast').filter(has_text='已添加 1 个模型')).to_be_visible()
            expect(choice).to_be_checked()
            expect(page.locator('#stgProviderModelPickerCount')).to_have_text('已选择 1 个模型')
            expect(custom).to_have_value('later-custom')
            expect(search).to_be_focused()
            assert any(row['model'] and row['model']['model_id'] == 'beta' and row['provider_access_id'] == 'access-a'
                       for row in state['document']['offerings'])
            page.locator('[data-tofu-action="_closeProviderModelPicker()"]').click()
            page.locator('#settingsModal .settings-close-btn').click()
            expect(page.locator('#stgProviderModelPickerOverlay')).to_have_count(0)
            assert not errors
            return

        if scenario == 'disabled-model-face':
            provider.locator('[data-tofu-action*="_openProviderModelPicker"]').click()
            page.locator('[data-model-key="creator::beta"]').check()
            page.locator('[data-tofu-action="_stageV2CheckedCatalogModels()"]').click()
            expect(page.locator('.app-dialog-overlay')).to_contain_text('disabled-face')
            assert not state['writes']
            page.locator('.app-dialog-overlay button').click()
            page.locator('[data-tofu-action="_closeProviderModelPicker()"]').click()
            # A clean draft skips PUT. An unrelated edit forces a real later
            # save, which must not publish a hidden partial Offering.
            name = provider.locator('input[data-tofu-action-change*="provider_accesses"]').first
            name.fill('Provider A renamed')
            name.press('Tab')
            with page.expect_request(lambda r: r.method == 'PUT' and urlparse(r.url).path == '/api/v4/model-routing'):
                page.locator('#settingsSaveBtn').click()
            expect(page.locator('#settingsModal')).not_to_be_visible(timeout=20000)
            saved = state['writes'][-1]['model_routing']
            assert next(row for row in saved['provider_accesses']
                        if row['provider_id'] == 'provider-a')['display_name'] == 'Provider A renamed'
            assert saved['offerings'] == document['offerings']
            assert saved['deployments'] == document['deployments']
            assert saved['credentials'] == document['credentials']
            assert saved['connections'] == document['connections']
            assert not errors
            return
        if scenario != 'normal':
            if scenario == 'required-selector':
                wire.get_by_role('button', name='alpha', exact=True).click()
                page.locator('.stg-rnp-header-row button').click()
                page.locator('.stg-rnp-input').first.press('Enter')
                expect(page.locator('.app-dialog-overlay')).to_be_visible()
                expect(page.locator('.app-dialog-overlay')).to_contain_text('x-selector')
                expect(provider.locator('.is-alias[data-id="alpha"] .selector')).to_have_count(1)
            else:
                provider.locator('[data-tofu-action*="_addV2Alias"]').first.click()
                page.locator('.stg-rnp-input').first.fill('face-alias')
                page.locator('.stg-rnp-input').first.press('Enter')
                expect(page.locator('.app-dialog-overlay')).to_be_visible()
                expect(page.locator('.app-dialog-overlay')).to_contain_text('disabled-face')
                expect(provider.locator('.is-alias[data-id="face-alias"]')).to_have_count(0)
            page.screenshot(path=str(tmp_path / f'provider-{scenario}.png'))
            assert not state['writes']
            assert not errors
            return
        wire.get_by_role('button', name='alpha', exact=True).click()
        page.locator('.stg-rnp-input').first.fill('provider-alpha')
        page.locator('.stg-rnp-input').first.press('Enter')
        expect(provider.locator('.is-alias[data-id="provider-alpha"]')).to_have_count(1)
        provider.locator('.is-alias[data-id="provider-alpha"] .stg-mx-act.del').click()
        expect(provider.locator('.is-logical[data-id="alpha"]')).to_have_count(1)
        expect(provider.locator('.is-alias')).to_have_count(0)
        provider.locator('[data-tofu-action*="_openProviderModelPicker"]').click()
        page.locator('[data-model-key="creator::beta"]').check()
        page.locator('[data-tofu-action="_stageV2CheckedCatalogModels()"] ').click()
        expect(provider.locator('.is-logical[data-id="beta"]')).to_have_count(1)
        expect(provider.locator('.is-alias[data-id="beta-provider-wire"]')).to_have_count(1)
        assert state['writes']
        assert [row['wire_model_id'] for row in state['document']['deployments']] == ['beta-provider-wire']
        page.locator('[data-tofu-action="_closeProviderModelPicker()"] ').click()
        page.screenshot(path=str(tmp_path/'provider-request-names.png'))
        page.set_viewport_size({'width': 390, 'height': 844})
        expect(provider.locator('.is-alias[data-id="beta-provider-wire"]')).to_be_visible()
        assert not errors
    finally:
        context.close()


def _credential_replacement_journey(page, provider, state, metadata_changed):
    from playwright.sync_api import expect

    def acknowledge():
        state['revision'] += 1
        state['document']['revision'] = state['revision']
        row = state['document']['credentials'][0]
        row.update(secret_reference=f"synthetic-reference-{state['revision']}", key_hint='synt****saved')
        state.pop('pending_credential').fulfill(json={'ok': True, 'revision': state['revision'],
            'credential_id': row['credential_id'], 'key_hint': row['key_hint']})

    secret = provider.locator('.stg-keys-input').first
    provider.locator('.stg-key-reveal').first.click()
    expect(secret).to_have_value('synthetic-original')
    secret.fill('synthetic-first')
    save = page.locator('#settingsSaveBtn')
    with page.expect_request(lambda request: urlparse(request.url).path.endswith('/secret')):
        save.click()
    expect(save).to_be_disabled()
    secret.fill('synthetic-second')
    acknowledge()
    expect(save).to_be_enabled()
    expect(page.locator('#settingsModal')).to_be_visible()
    expect(page.locator('#settingsStatusHint')).to_contain_text('有未保存的更改')
    expect(secret).to_be_focused()
    expect(secret).to_have_value('synthetic-second')
    secret.fill('synthetic-third')
    expect(secret).to_have_value('synthetic-third')
    with page.expect_request(lambda request: urlparse(request.url).path.endswith('/secret')):
        save.click()
    assert [body['secret'] for body in state['credential_writes']] == ['synthetic-first', 'synthetic-third']
    acknowledge()
    expect(page.locator('#settingsModal')).not_to_be_visible()
    assert len(state['writes']) == int(metadata_changed), 'only independently edited metadata needs a whole-document PUT'

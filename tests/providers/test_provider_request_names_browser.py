"""Real Settings page keeps Model identity separate from every wire name."""
import json
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['normal', 'required-selector', 'disabled-face'])
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

    face_url = 'https://fixture.example/face'
    if scenario == 'required-selector':
        document['connections'][0]['extra_headers'] = {'x-selector': 'required'}
    if scenario == 'disabled-face':
        document['connections'].append({**document['connections'][0], 'connection_id': 'disabled-face',
            'base_url': face_url, 'gateway_namespace': 'fixture', 'enabled': False})
    state = {'document': document, 'revision': document['revision'], 'writes': []}
    context = browser.new_context(viewport={'width': 1280, 'height': 1000}, service_workers='block')
    context.add_init_script('localStorage.setItem("claude_client_config", ' + json.dumps(json.dumps({
        'model': 'alpha', 'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'}, 'thinkingDepth': 'default'})) + ');')

    def handle(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc: return route.abort()
        if url.path == '/api/v4/model-routing':
            if route.request.method == 'PUT':
                body = route.request.post_data_json
                assert body['expected_revision'] == state['revision']
                state['document'] = prepare_registration_document(body['model_routing'], previous=state['document'])
                state['revision'] += 1
                state['document']['revision'] = state['revision']
                state['writes'].append(body)
            return route.fulfill(json={'ok': True, 'revision': state['revision'], 'model_routing': state['document'],
                'metadata_inheritance_supported': True, 'reasoning_controls': reasoning_controls(state['document'])})
        if url.path == '/api/v4/providers/templates':
            return route.fulfill(json={'ok': True, 'items': [{'key': 'test-template', 'name': 'Provider A', 'category': 'relay',
                'base_url': document['connections'][0]['base_url'],
                'faces': {'fixture': {'base_url': face_url if scenario == 'disabled-face' else document['connections'][0]['base_url'],
                    'protocol': 'openai', 'extra_headers': {'x-selector': 'required'}}},
                'offering_recipes': [{'model_id': 'alpha', 'request_ids': ['alpha', 'face-alias'],
                    'request_faces': {'alpha': 'fixture', 'face-alias': 'fixture'}},
                    {'model_id': 'beta', 'request_ids': ['beta-provider-wire']}]}]})
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

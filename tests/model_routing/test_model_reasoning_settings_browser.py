"""Model draft edit → owner CAS → refreshed composer uses native effort ladder."""
from copy import deepcopy
import json
import re
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def test_models_tab_excludes_incomplete_xai_drafts(browser, live_server):
    from playwright.sync_api import expect
    from lib.model_routing.domain import empty_document

    routing = empty_document()
    routing['creators'] = [{'creator_id': 'xai', 'name': 'xAI'}]
    routing['models'] = [
        {'creator_id': 'xai', 'model_id': name, 'display_name': name,
         'capabilities': ['text'], 'context_window': 500_000,
         'quality_rank': 1, 'registration_state': state,
         'registration_issues': ['list_pricing.input/output/currency/unit'] if state != 'ready' else []}
        for name, state in (
            ('grok-4.7', 'ready'),
            ('grok-4.20-multi-agent', 'pending_metadata'),
            ('grok-4.3', 'pending_validation'),
        )
    ]
    context = browser.new_context(viewport={'width': 1280, 'height': 950}, service_workers='block')

    def route_request(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if url.path == '/api/v4/model-routing':
            return route.fulfill(json={'ok': True, 'revision': 1,
                'model_routing': routing, 'reasoning_controls': {},
                'metadata_inheritance_supported': True})
        if url.path == '/api/v4/model-intelligence/aa':
            return route.fulfill(json={'ok': True, 'aa': {'status': 'no_key', 'scores': {}}})
        if url.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        return route.continue_()

    context.route('**/*', route_request)
    page = context.new_page()
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        settings_button = page.locator('[data-tofu-action="openSettings()"]')
        settings_button.first.click()
        page.locator('.settings-tab[data-tab="models"]').click()
        page.locator('#stgModelCatalogSearch').fill('grok')
        cards = page.locator('#stgModelCatalog .stg-mc-model')
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text('grok-4.7')
        expect(page.locator('#stgModelCatalog')).not_to_contain_text('grok-4.20-multi-agent')
        expect(page.locator('#stgModelCatalog')).not_to_contain_text('grok-4.3')
    finally:
        context.close()


def test_model_reasoning_edit_save_and_provider_inheritance(browser, live_server, tmp_path):
    from playwright.sync_api import expect
    from lib.model_routing.controls import reasoning_controls
    from lib.model_routing.domain import normalize_document
    from tests.model_routing.test_model_routing_contract import _full_document

    document = _full_document()
    document['models'][0]['inference_profile'] = {'reasoning': {
        'support': 'supported', 'wire_format': 'reasoning_effort',
        'can_disable': False, 'supported_efforts': ['low', 'high', 'max'],
        'default_effort': 'max'}}
    document = normalize_document(document)
    original = deepcopy(document)
    state = {'document': document, 'revision': 1, 'writes': []}
    aa_requests = []
    aa_released = False
    context = browser.new_context(viewport={'width': 1280, 'height': 1000}, service_workers='block')
    config = {'model': 'alpha', 'modelRef': {'creator_id': 'creator', 'model_id': 'alpha'},
              'preferredProviderId': '', 'thinkingDepth': 'max'}
    context.add_init_script('localStorage.setItem("claude_client_config", '+json.dumps(json.dumps(config))+');')

    def route_request(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if url.path == '/api/v4/model-routing':
            if route.request.method == 'PUT':
                request = route.request.post_data_json
                assert request['expected_revision'] == state['revision']
                state['document'] = normalize_document(request['model_routing'])
                state['revision'] += 1
                state['document']['revision'] = state['revision']
                state['writes'].append(request)
            return route.fulfill(json={'ok': True, 'revision': state['revision'],
                'model_routing': state['document'], 'metadata_inheritance_supported': True,
                'reasoning_controls': reasoning_controls(state['document'])})
        if url.path == '/api/v4/update/check':
            return route.fulfill(json={'update_available': False})
        if url.path == '/api/v4/model-intelligence/aa':
            if aa_released:
                return route.fulfill(json={'ok': True, 'aa': {'status': 'ok', 'scores': {}}})
            aa_requests.append(route)
            return None
        if '/chat' in url.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    context.route('**/*', route_request)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.wait_for_selector('#presetDropdownList [data-value="alpha"]', state='attached')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        with page.expect_request(lambda r: urlparse(r.url).path == '/api/v4/model-intelligence/aa'):
            page.locator('[data-tofu-action="openSettings()"]').first.click()
            page.locator('.settings-tab[data-tab="models"]').click()
        page.locator('#stgModelCatalogSearch').fill('alpha')
        card = page.locator('.stg-mc-model').first
        card.locator('> summary').click()
        editor = card.locator('.stg-reasoning-editor')
        editor.locator('summary').click()
        expect(editor.locator('[name="efforts"]')).to_have_value('low, high, max')
        expect(editor.locator('[name="canDisable"]')).to_have_value('false')
        editor.locator('[name="efforts"]').fill('low, high, max, ultra')
        editor.locator('[name="defaultEffort"]').fill('ultra')
        # AA finishes while the user is editing: preserve open form and text.
        assert aa_requests
        aa_released = True
        for route in aa_requests:
            route.fulfill(json={'ok': True, 'aa': {'status': 'ok', 'scores': {}}})
        expect(editor.locator('[name="efforts"]')).to_have_value('low, high, max, ultra')
        expect(editor.locator('[name="defaultEffort"]')).to_be_focused()
        editor.locator('button').first.click()
        assert not state['writes'], 'Edit must stay in the existing Settings draft'
        page.screenshot(path=str(tmp_path/'model-reasoning-staged.png'))
        with page.expect_request(lambda r: r.method == 'PUT' and urlparse(r.url).path == '/api/v4/model-routing'):
            page.locator('#settingsSaveBtn').click()
        expect(page.locator('#settingsModal')).not_to_be_visible(timeout=20000)
        saved = state['document']['models'][0]['inference_profile']['reasoning']
        assert saved['supported_efforts'] == ['low', 'high', 'max', 'ultra']
        assert saved['default_effort'] == 'ultra' and saved['can_disable'] is False
        assert state['document']['offerings'] == original['offerings']
        assert state['document']['credentials'] == original['credentials']
        page.locator('#presetToggle').click()
        slider = page.locator('#thinkingDepthRange')
        # Settings keeps the prior medium intent as an unavailable rung.
        # Select a supported rung before choosing the newly added maximum.
        expect(slider).to_have_attribute('max', '4')
        expect(slider).to_have_attribute('aria-valuetext', re.compile('不可用|Unavailable', re.I))
        slider.focus(); page.keyboard.press('Home')
        expect(slider).to_have_attribute('max', '3')
        page.keyboard.press('End')
        assert page.evaluate('JSON.parse(localStorage.getItem("claude_client_config")).thinkingDepth') == 'ultra'
        assert page.locator('#thinkingDepthSection .depth-slider-dots').first.locator('span').count() == 4

        # Override one provider's ladder, then restore inheritance through the
        # same UI. The Model and sibling provider remain independent.
        def open_override():
            page.locator('[data-tofu-action="openSettings()"]').first.click()
            page.locator('.settings-tab[data-tab="api"]').click()
            provider = page.locator('.stg-provider-card-v2[data-provider-id="provider-a"]')
            if not provider.evaluate('(el) => el.open'):
                provider.locator('> summary').click()
            group = provider.locator('.stg-provider-body > .stg-reasoning-editor')
            group.locator('> summary').click()
            override = group.locator('[data-offering-id="alpha-a"]')
            override.locator('> summary').click()
            return override
        override = open_override()
        override.locator('[name="efforts"]').fill('low, high, max')
        override.locator('[name="defaultEffort"]').fill('max')
        override.locator('button').first.click()
        with page.expect_request(lambda r: r.method == 'PUT' and urlparse(r.url).path == '/api/v4/model-routing'):
            page.locator('#settingsSaveBtn').click()
        expect(page.locator('#settingsModal')).not_to_be_visible(timeout=20000)
        offers = {o['offering_id']: o for o in state['document']['offerings']}
        assert offers['alpha-a']['inference_profile']['reasoning']['supported_efforts'] == ['low', 'high', 'max']
        assert not offers['alpha-b'].get('inference_profile')
        assert state['document']['models'][0]['inference_profile']['reasoning']['default_effort'] == 'ultra'
        override = open_override()
        override.locator('button').last.click()
        expect(override.locator('[name="efforts"]')).to_have_value('low, high, max, ultra')
        with page.expect_request(lambda r: r.method == 'PUT' and urlparse(r.url).path == '/api/v4/model-routing'):
            page.locator('#settingsSaveBtn').click()
        expect(page.locator('#settingsModal')).not_to_be_visible(timeout=20000)
        restored = next(o for o in state['document']['offerings'] if o['offering_id'] == 'alpha-a')
        assert not restored.get('inference_profile', {}).get('reasoning')
        assert not errors, errors
    except Exception:
        page.screenshot(path=str(tmp_path/'reasoning-settings-failure.png'))
        raise
    finally:
        context.close()

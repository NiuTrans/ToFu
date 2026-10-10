"""Real local Tofu app wiring; synthetic routing response, no model requests."""
import json
import os
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def _install_browser_config(context, config):
    # Match the explicit test server policy; production and the v4 lane use v4.
    # The shared legacy fixture defaults to v1 for older API-only tests.
    v4 = os.environ.get('TOFU_API_MAJOR', '4') == '4'
    context.add_init_script('window.TOFU_API_V4 = ' + json.dumps(v4)
        + '; localStorage.setItem("claude_client_config", '
        + json.dumps(json.dumps(config)) + ');')


def _api_path(url):
    return urlparse(url).path.replace('/api/v4/', '/api/v4/', 1)


def _fulfill_success(route, payload):
    if urlparse(route.request.url).path.startswith('/api/v4/'):
        payload = {'data': payload, 'meta': {}}
    return route.fulfill(json=payload)


def test_reasoning_controls_real_app(browser, live_server, tmp_path):
    from playwright.sync_api import expect
    from lib.llm.reasoning import reasoning_depth_restoration

    artifacts = tmp_path
    document = {
        'contract_version': 'tofu.model-routing/v2',
        'creators': [{'creator_id': c, 'name': c} for c in ['Creator A', 'Creator B']],
        'models': [{'creator_id': c, 'model_id': 'synthetic-depth'} for c in ['Creator A', 'Creator B']],
        'providers': [{'provider_id': p, 'name': p} for p in ['Provider A', 'Provider B']],
        'provider_accesses': [{'provider_access_id': p, 'provider_id': p, 'enabled': True} for p in ['Provider A', 'Provider B']],
        'offerings': [], 'deployments': [],
    }
    for i, (creator, provider) in enumerate([('Creator A', 'Provider A'), ('Creator A', 'Provider B'), ('Creator B', 'Provider A')]):
        document['offerings'].append({'offering_id': str(i), 'provider_access_id': provider, 'enabled': True,
            'identity_state': 'confirmed', 'model': {'creator_id': creator, 'model_id': 'synthetic-depth'}, 'capabilities': ['chat', 'thinking']})
        document['deployments'].append({'offering_id': str(i)})
    controls = []
    for creator, provider, depths in [('Creator A', '', ['lowest', 'high']), ('Creator A', 'Provider A', ['lowest', 'high']),
                                     ('Creator A', 'Provider B', ['high']), ('Creator B', '', [])]:
        controls.append({'creator_id': creator, 'model_id': 'synthetic-depth', 'provider_id': provider, 'offering_id': '',
                         'support': 'supported' if depths else 'unsupported', 'can_disable': False, 'depths': depths,
                         **({'restoration': reasoning_depth_restoration(depths)} if depths else {})})
    context = browser.new_context(viewport={'width': 1280, 'height': 900}, service_workers='block')
    errors, seen, blocked = [], [], []
    def route_request(route):
        url = route.request.url
        if urlparse(url).netloc != urlparse(live_server).netloc:
            blocked.append(url)
            return route.abort()
        if _api_path(url) == '/api/v4/model-routing':
            assert route.request.method == 'GET'
            seen.append(url)
            return _fulfill_success(route, {'model_routing': document, 'reasoning_controls': controls})
        if _api_path(url) == '/api/v4/update/check':
            return _fulfill_success(route, {'update_available': False})
        if '/chat' in urlparse(url).path and route.request.method == 'POST':
            raise AssertionError('Unexpected model submission')
        return route.continue_()
    context.route('**/*', route_request)
    config = {'model': 'synthetic-depth', 'modelRef': {'creator_id': 'Creator A', 'model_id': 'synthetic-depth'},
              'preferredProviderId': '', 'thinkingDepth': 'lowest', 'defaultThinkingDepth': 'lowest'}
    _install_browser_config(context, config)
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    def saved():
        return page.evaluate('JSON.parse(localStorage.getItem("claude_client_config"))')
    def open_picker():
        if not page.locator('#thinkingDepthRange').is_visible():
            page.locator('#presetToggle').click()
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.wait_for_selector('#presetDropdownList [data-value="synthetic-depth"]', state='attached')
        page.locator('#obSkip').click()
        open_picker()
        slider = page.locator('#thinkingDepthRange')
        expect(slider).to_have_attribute('max', '1')
        expect(slider).to_have_value('0')
        assert saved()['thinkingDepth'] == 'lowest'
        # Exercise the real retained adapter and persistence with repeated
        # mouse gestures; unit projection alone cannot see pointer capture.
        box = slider.bounding_box()
        y = box['y'] + box['height'] / 2
        for _ in range(2):
            page.mouse.move(box['x'] + box['width'] / 2, y)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width'] - 15, y, steps=8)
            page.mouse.up()
            expect(slider).to_be_visible()
            assert saved()['thinkingDepth'] == 'high'
            slider.focus()
            page.keyboard.press('ArrowLeft')
            assert saved()['thinkingDepth'] == 'lowest'
        slider.focus()
        page.locator('#thinkingDepthSection [data-depth-default]').click()
        assert saved()['thinkingDepth'] == 'default'
        slider.focus()
        page.keyboard.press('End')
        page.keyboard.press('Home')
        assert saved()['thinkingDepth'] == 'lowest'
        creator_a = page.locator('#presetDropdownList [data-route-key="auto::Creator A::::synthetic-depth"]').first
        creator_a.locator('.ps-dd-provider-chip').click()
        page.locator('#psProviderMenu').get_by_text('Provider B', exact=True).click()
        open_picker()
        expect(slider).to_have_attribute('max', '1')
        expect(slider).to_be_disabled()
        expect(slider).to_have_attribute('aria-valuetext', __import__('re').compile('Unavailable|不可用'))
        assert saved()['thinkingDepth'] == 'lowest'
        assert saved()['preferredProviderId'] == 'Provider B'
        page.screenshot(path=str(artifacts/'provider-unavailable.png'))
        page.locator('#presetDropdownList [data-route-key="auto::Creator B::::synthetic-depth"]').first.click()
        open_picker()
        expect(slider).to_have_attribute('max', '1')
        expect(slider).to_have_attribute('aria-valuetext', __import__('re').compile('Unavailable|不可用'))
        assert saved()['thinkingDepth'] == 'lowest'
        assert saved()['modelRef']['creator_id'] == 'Creator B'
        page.screenshot(path=str(artifacts/'creator-unavailable.png'))
        creator_a.locator('.ps-dd-provider-chip').click()
        page.locator('#psProviderMenu').get_by_text('Provider A', exact=True).click()
        open_picker()
        expect(slider).to_be_enabled()
        expect(slider).to_have_value('0')
        assert saved()['thinkingDepth'] == 'lowest'
        assert seen, 'Full app never requested routing authority'
        assert not errors, errors
        print(f'REAL APP {live_server}; routing GETs={len(seen)}; external blocked={len(blocked)}; screenshots={artifacts}', flush=True)
    except Exception:
        page.screenshot(path=str(artifacts/'failure.png'))
        print(f'Failure screenshot: {artifacts}/failure.png; page errors: {errors}', flush=True)
        raise
    finally:
        context.close()


@pytest.mark.parametrize('depth', ['max', 'default'])
def test_unknown_capability_does_not_submit_a_disabled_reasoning_override(browser, live_server, depth):
    """Rendering unknown capability must not overwrite the saved user choice."""
    from playwright.sync_api import expect
    from lib.llm.reasoning import capture_intent, compile_reasoning

    ref = {'creator_id': 'moonshot', 'model_id': 'kimi-k3'}
    document = {
        'contract_version': 'tofu.model-routing/v2',
        'creators': [{'creator_id': 'moonshot', 'name': 'Moonshot'}],
        'models': [ref],
        'providers': [{'provider_id': 'synthetic', 'name': 'Synthetic'}],
        'provider_accesses': [{'provider_access_id': 'access', 'provider_id': 'synthetic', 'enabled': True}],
        'offerings': [{'offering_id': 'offering', 'provider_access_id': 'access',
                      'enabled': True, 'identity_state': 'confirmed', 'model': ref,
                      'capabilities': ['text', 'thinking']}],
        'deployments': [{'offering_id': 'offering'}],
    }
    controls = [{**ref, 'provider_id': '', 'offering_id': '', 'support': 'unknown',
                 'can_disable': None, 'depths': ['default']}]
    context = browser.new_context(viewport={'width': 1280, 'height': 900}, service_workers='block')
    config = {'model': 'kimi-k3', 'modelRef': ref, 'preferredProviderId': '',
              'thinkingDepth': depth, 'defaultThinkingDepth': depth, 'autoTranslate': False}
    _install_browser_config(context, config)
    submissions, resolver_inputs, errors = [], [], []

    def route_request(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if _api_path(route.request.url) == '/api/v4/model-routing':
            return _fulfill_success(route, {'model_routing': document, 'reasoning_controls': controls})
        if _api_path(route.request.url) == '/api/v4/update/check':
            return _fulfill_success(route, {'update_available': False})
        if route.request.method == 'POST':
            body = route.request.post_data_json
            if isinstance(body, dict) and body.get('include_settings'):
                resolver_inputs.append(body)
            if url.path.endswith('/turns'):
                submissions.append(body)
                # Stop at browser ingress; never launch a model or persist a
                # synthetic conversation on the otherwise real local server.
                return route.fulfill(status=400, json={'ok': False, 'error': 'Synthetic submission captured'})
        return route.continue_()

    context.route('**/*', route_request)
    page = context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.wait_for_selector('#presetDropdownList [data-value="kimi-k3"]', state='attached')
        page.locator('#obSkip').click()
        page.locator('.new-chat-btn').click()
        page.locator('#presetToggle').click()
        page.locator('#presetDropdownList [data-value="kimi-k3"]').first.click()
        page.locator('#userInput').fill('Synthetic reasoning submission')
        with page.expect_request(lambda request: request.method == 'POST'
                                 and urlparse(request.url).path.endswith('/turns')):
            page.locator('#sendBtn').click()
        expect(page.locator('#sendBtn')).not_to_have_class(__import__('re').compile('stop-btn'))
        assert resolver_inputs and submissions
        assert 'thinkingEnabled' not in resolver_inputs[-1]['overrides']
        assert resolver_inputs[-1]['overrides']['thinkingDepth'] == depth
        submitted = submissions[-1]['config']
        assert submitted['thinkingDepth'] == depth
        intent = capture_intent(thinking_enabled=submitted['thinkingEnabled'],
                                thinking_depth=submitted['thinkingDepth'])
        assert intent['mode'] == ('preset' if depth == 'max' else 'default')
        body = {'_reasoning_intent': intent}
        compile_reasoning(body, 'kimi-k3', inference_profile={})
        assert body.get('reasoning_effort') == ('max' if depth == 'max' else None)
        assert not errors, errors
    finally:
        context.close()


@pytest.mark.parametrize('state', ['no_route', 'unknown', 'restore'])
def test_missing_reasoning_controls_are_explained_in_real_app(browser, live_server, tmp_path, state):
    from playwright.sync_api import expect
    from lib.model_routing.controls import reasoning_controls
    from tests.model_routing.test_model_routing_contract import _full_document
    document = _full_document()
    if state == 'restore':
        from tests.model_routing.test_model_reasoning_controls import _document
        document = _document()
    elif state == 'no_route':
        document['models'][0]['inference_profile'] = {'reasoning': {
            'support': 'supported', 'wire_format': 'thinking_type', 'enabled_type': 'adaptive',
            'can_disable': True, 'supported_efforts': ['low', 'medium', 'high', 'xhigh', 'max'],
            'default_effort': 'high', 'effort_field': 'effort'}}
        for credential in document['credentials']:
            credential['authorization']['models'] = []
    controls = reasoning_controls(document)
    context = browser.new_context(viewport={'width': 1100, 'height': 900}, service_workers='block')
    config = {'model':'alpha', 'modelRef':{'creator_id':'creator','model_id':'alpha'},
              'thinkingDepth':'default', 'defaultThinkingDepth':'default', 'preferredProviderId':''}
    if state == 'restore':
        config.update(thinkingDepth='ultra', defaultThinkingDepth='ultra')
    _install_browser_config(context, config)
    def route_request(route):
        url = urlparse(route.request.url)
        if url.netloc != urlparse(live_server).netloc:
            return route.abort()
        if _api_path(route.request.url) == '/api/v4/model-routing':
            assert route.request.method == 'GET'
            return _fulfill_success(route, {'model_routing':document, 'reasoning_controls':controls})
        if _api_path(route.request.url) == '/api/v4/update/check':
            return _fulfill_success(route, {'update_available':False})
        if '/chat' in url.path and route.request.method == 'POST':
            raise AssertionError('Controls must never probe or change authorization')
        return route.continue_()
    context.route('**/*', route_request)
    page = context.new_page()
    errors=[]
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_selector('#presetDropdownList [data-value="alpha"]', state='attached')
        if page.locator('#obSkip').is_visible():
            page.locator('#obSkip').click()
        page.locator('#presetToggle').click()
        slider=page.locator('#thinkingDepthRange')
        expect(slider).to_be_visible()
        note=page.locator('#thinkingDepthSection [data-depth-notice]')
        if state == 'restore':
            expect(slider).to_be_enabled()
            expect(slider).to_have_attribute('max', '2')
            expect(slider).to_have_value('2')
            expect(slider).to_have_attribute('aria-valuetext', __import__('re').compile('Unavailable|不可用'))
            expect(note).to_be_hidden()
        else:
            expect(slider).to_be_disabled()
            expect(slider).to_have_attribute('max', '5' if state == 'no_route' else '0')
            expect(note).to_be_visible()
            expect(note).to_contain_text('授权' if state == 'no_route' else '尚未配置')
        page.screenshot(path=str(tmp_path/(state+'.png')))
        assert page.evaluate('JSON.parse(localStorage.getItem("claude_client_config")).thinkingDepth') == ('ultra' if state == 'restore' else 'default')
        assert not errors, errors
    finally:
        context.close()

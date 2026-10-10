"""Browser contract: startup failures recover and late locales repaint picker UI."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


def _picker_catalog():
    from lib.model_routing.domain import normalize_document
    from tests.model_routing.test_model_routing_contract import _full_document

    catalog = _full_document()
    catalog['models'][0]['inference_profile'] = {'reasoning': {
        'support': 'supported', 'wire_format': 'reasoning_effort',
        'can_disable': False, 'supported_efforts': ['low', 'high', 'max'],
        'default_effort': 'max'}}
    return normalize_document(catalog)


@pytest.mark.parametrize('failure', ['server_booting', 'connection', 'unavailable', 'gateway'])
def test_booting_picker_recovers_on_ready_without_reload(browser, live_server, failure):
    from playwright.sync_api import expect
    from lib.model_routing.controls import reasoning_controls

    catalog = _picker_catalog()
    state = {'booting': True, 'config_calls': 0, 'routing_calls': 0}
    context = browser.new_context(viewport={'width': 1280, 'height': 950},
                                  service_workers='block')
    page = context.new_page()
    js_errors = []
    page.on('pageerror', lambda error: js_errors.append(str(error)))

    def intercept(route):
        path = urlparse(route.request.url).path
        if path in ('/api/v4/server-config', '/api/v4/model-routing'):
            key = 'config_calls' if path.endswith('server-config') else 'routing_calls'
            state[key] += 1
            if state['booting']:
                if failure == 'connection':
                    return route.abort('connectionrefused')
                code = 'server_booting' if failure == 'server_booting' else 'database_unavailable'
                return route.fulfill(status=502 if failure == 'gateway' else 503, json={
                    'ok': False, 'error': {'code': code, 'message': 'Restarting'},
                    'error_code': code})
            if key == 'routing_calls':
                return route.fulfill(json={'ok': True, 'model_routing': catalog,
                                           'reasoning_controls': reasoning_controls(catalog)})
        if path == '/api/v4/boot' and state['booting'] and failure == 'server_booting':
            phase = {'index': 1, 'key': 'storage_sidecar', 'label': 'Storage',
                     'status': 'running', 'duration_s': None, 'error': None}
            return route.fulfill(json={'ok': True, 'status': 'running',
                                       'phaseTotal': 1, 'phases': [phase],
                                       'currentPhase': phase, 'failedPhase': None,
                                       'retryCount': 0, 'elapsedS': 1, 'error': None})
        return route.continue_()

    context.route('**/*', intercept)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.wait_for_function('window.__tofuBootSplash && '
                               'document.querySelector("#presetDropdownList")')
        page.wait_for_function('document.querySelector("#presetDropdownList") && '
                               'document.querySelector("#presetDropdownList").textContent.includes("加载")',
                               timeout=10_000)
        # Startup may already have retried while the browser was rendering.
        # Exact single-flight/timer bounds belong to the fake-clock unit spec.
        assert state['config_calls'] >= 1
        assert state['routing_calls'] >= 1
        assert page.locator('#presetDropdownList .preset-dropdown-item').count() == 0

        state['booting'] = False
        # Boot-gate recovery uses readiness; ordinary restart failures have
        # already missed that event and must recover via their own lifecycle.
        identity = page.evaluate('window.__pickerDocumentIdentity = Math.random()')
        expect(page.locator('#presetDropdownList [data-value="alpha"]')).to_be_attached(timeout=10_000)
        assert state['config_calls'] >= 2
        assert state['routing_calls'] >= 2
        assert page.evaluate('window.__pickerDocumentIdentity') == identity
        expect(page.locator('#presetDropdownList')).not_to_contain_text('toolbar.retryLoadingModels')
        expect(page.locator('#presetDropdownList .ps-dd-loading')).to_have_count(0)
        page.locator('#presetToggle').click()
        page.locator('#presetDropdownList [data-value="alpha"]').first.click()
        page.locator('#presetToggle').click()
        expect(page.locator('#thinkingDepthSection')).to_be_visible()
        expect(page.locator('#thinkingDepthRange')).to_have_attribute('max', '3')
        assert not js_errors, js_errors
    finally:
        context.close()


def test_failed_picker_repaints_when_delayed_locale_arrives(browser, live_server):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={'width': 1280, 'height': 950},
                                  service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en')")
    page = context.new_page()
    js_errors = []
    page.on('pageerror', lambda error: js_errors.append(str(error)))
    pending_locales = []
    state = {'locale_released': False, 'routing_calls': 0}

    def delay_locale(route):
        if state['locale_released']:
            return route.continue_()
        pending_locales.append(route)

    def fail_catalog(route):
        if urlparse(route.request.url).path.endswith('model-routing'):
            state['routing_calls'] += 1
        return route.fulfill(status=503, json={
            'ok': False, 'error_code': 'database_unavailable',
            'error': {'code': 'database_unavailable', 'message': 'Restarting'}})

    # i18n/index.ts fetches Vite-emitted generated JSON URLs, not /locales/*.json.
    # Match both hashed build assets and un-hashed generated development URLs.
    context.route(re.compile(r'/(?:en|zh)\.generated(?:-[^/?]+)?\.json(?:\?.*)?$'),
                  delay_locale)
    context.route(re.compile(r'/api/v4/(?:server-config|model-routing)(?:\?.*)?$'),
                  fail_catalog)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        retry_row = page.locator('#presetDropdownList button.ps-dd-loading')
        expect(retry_row).to_have_text('toolbar.retryLoadingModels', timeout=10_000)
        assert pending_locales, 'The real locale fetch must be held before startup'
        assert state['routing_calls'] >= 1
        identity = page.evaluate('window.__pickerDocumentIdentity = Math.random()')

        state['locale_released'] = True
        for route in pending_locales:
            route.continue_()
        pending_locales.clear()
        # Keep both endpoints failing: translation must repaint the error row,
        # not merely disappear when a successful catalog replaces the dropdown.
        expect(retry_row).to_have_text('Could not load models — retry', timeout=10_000)
        expect(retry_row).to_have_attribute('data-i18n', 'toolbar.retryLoadingModels')
        expect(page.locator('#presetDropdownList .preset-dropdown-item')).to_have_count(0)
        assert page.evaluate('window.__pickerDocumentIdentity') == identity
        assert not js_errors, js_errors
    finally:
        context.close()


def test_timed_out_picker_recovers_without_reload(browser, live_server):
    from playwright.sync_api import expect
    from lib.model_routing.controls import reasoning_controls

    catalog = _picker_catalog()
    context = browser.new_context(viewport={'width': 1280, 'height': 950},
                                  service_workers='block')
    context.add_init_script("""(() => {
        localStorage.setItem('tofu_ui_lang', 'en');
        const nativeFetch = window.fetch;
        window.fetch = function(input, options) {
            const url = new URL(typeof input === 'string' ? input : input.url, location.href);
            if (url.pathname === '/api/v4/model-routing' && options?.signal) {
                options.signal.addEventListener('abort', () => {
                    window.__pickerAbortCode = options.signal.reason?.code;
                }, { once: true });
            }
            return nativeFetch.apply(this, arguments);
        };
    })()""")
    page = context.new_page()
    js_errors = []
    page.on('pageerror', lambda error: js_errors.append(str(error)))
    pending_routes = []
    state = {'routing_calls': 0, 'recovered': False}

    def routing(route):
        state['routing_calls'] += 1
        if state['routing_calls'] == 1:
            # Retain the actual browser request, preserving the transport's
            # production 15-second AbortSignal deadline rather than faking it.
            pending_routes.append(route)
            return
        if not state['recovered']:
            return route.fulfill(status=503, json={
                'ok': False, 'error_code': 'database_unavailable'})
        return route.fulfill(json={'ok': True, 'model_routing': catalog,
                                   'reasoning_controls': reasoning_controls(catalog)})

    context.route('**/api/v4/model-routing', routing)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        identity = page.evaluate('window.__pickerDocumentIdentity = Math.random()')
        page.wait_for_function("window.__pickerAbortCode === 'timeout'", timeout=20_000)
        assert pending_routes, 'The catalog request must reach its real deadline'
        # Chromium can defer delivery of cancellation while a route is paused.
        # Release only after the client's deadline; the transport must classify
        # this as timeout and the coordinator must allow a subsequent request.
        for route in pending_routes:
            route.abort('timedout')
        pending_routes.clear()
        expect(page.locator('#presetDropdownList button.ps-dd-loading')).to_have_text(
            'Could not load models — retry', timeout=5_000)
        expect(page.locator('#presetDropdownList .preset-dropdown-item')).to_have_count(0)

        state['recovered'] = True
        expect(page.locator('#presetDropdownList [data-value="alpha"]')).to_be_attached(
            timeout=10_000)
        assert state['routing_calls'] >= 2
        assert page.evaluate('window.__pickerDocumentIdentity') == identity
        expect(page.locator('#presetDropdownList .ps-dd-loading')).to_have_count(0)
        page.locator('#presetToggle').click()
        page.locator('#presetDropdownList [data-value="alpha"]').first.click()
        page.locator('#presetToggle').click()
        expect(page.locator('#thinkingDepthSection')).to_be_visible()
        expect(page.locator('#thinkingDepthRange')).to_have_attribute('max', '3')
        assert not js_errors, js_errors
    finally:
        context.close()


def test_picker_recovers_to_real_backend_without_reload(browser, live_server):
    from playwright.sync_api import expect

    context = browser.new_context(viewport={'width': 1280, 'height': 950},
                                  service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en')")
    page = context.new_page()
    js_errors = []
    page.on('pageerror', lambda error: js_errors.append(str(error)))
    state = {'unavailable': True, 'routing_calls': 0}

    def routing(route):
        state['routing_calls'] += 1
        if state['unavailable']:
            return route.fulfill(status=503, json={
                'ok': False, 'error_code': 'database_unavailable',
                'error': {'code': 'database_unavailable', 'message': 'Restarting'}})
        # Success is served by the fixture's real v4 route/owner repository,
        # not by the synthetic catalog used in the focused effort UI cases.
        return route.continue_()

    context.route('**/api/v4/model-routing', routing)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        expect(page.locator('#presetDropdownList button.ps-dd-loading')).to_have_text(
            'Could not load models — retry', timeout=10_000)
        identity = page.evaluate('window.__pickerDocumentIdentity = Math.random()')
        with page.expect_response(lambda response: (
                urlparse(response.url).path == '/api/v4/model-routing'
                and response.status == 200), timeout=10_000) as recovered:
            state['unavailable'] = False
        payload = recovered.value.json()
        assert payload['data']['model_routing']['contract_version'] == 'tofu.model-routing/v2'
        page.wait_for_function('''() => document.querySelector(
            '#presetDropdownList .preset-dropdown-item, '
            + '#presetDropdownList [data-i18n="toolbar.noModelsAvailable"]')''',
            timeout=10_000)
        expect(page.locator('#presetDropdownList button.ps-dd-loading')).to_have_count(0)
        expect(page.locator('#presetDropdownList')).not_to_contain_text('toolbar.')
        assert state['routing_calls'] >= 2
        assert page.evaluate('window.__pickerDocumentIdentity') == identity
        assert not js_errors, js_errors
    finally:
        context.close()


def test_recent_models_refresh_after_pick_and_other_tab_use(browser, live_server):
    import copy
    from playwright.sync_api import expect
    from lib.model_routing.domain import normalize_document
    from tests.model_routing.test_model_routing_contract import _full_document

    catalog = _full_document()
    for index in range(8):
        model_id = f'recent-test-{index}'
        model = copy.deepcopy(catalog['models'][1])
        model.update(model_id=model_id, display_name=model_id)
        catalog['models'].append(model)
        offering = copy.deepcopy(catalog['offerings'][2])
        offering.update(offering_id=model_id)
        offering['model']['model_id'] = model_id
        catalog['offerings'].append(offering)
        deployment = copy.deepcopy(catalog['deployments'][0])
        deployment.update(deployment_id=model_id, offering_id=model_id,
                          wire_model_id=model_id)
        catalog['deployments'].append(deployment)
    catalog = normalize_document(catalog)
    context = browser.new_context(viewport={'width': 1280, 'height': 950},
                                  service_workers='block')
    context.add_init_script("""if (!localStorage.getItem('tofu_recent_models')) {
        localStorage.setItem('tofu_recent_models', '["beta","alpha"]');
    }""")
    context.route('**/api/v4/model-routing', lambda route: route.fulfill(
        json={'ok': True, 'model_routing': catalog}))
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    recent_selector = '#presetDropdownList .preset-dropdown-item[data-section=\'["recent"]\']'
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        expect(page.locator('#presetDropdownList [data-value="recent-test-0"]').first).to_be_attached()
        page.locator('#presetToggle').click()
        expect(page.locator(recent_selector)).to_have_count(2)
        page.locator('#presetDropdownList [data-value="recent-test-0"]').first.click()
        # Explicitly close even if the picker intentionally remains open after a pick.
        page.locator('#userInput').click()
        page.locator('#presetToggle').click()
        expect(page.locator(recent_selector).first).to_have_attribute('data-value', 'recent-test-0')
        expect(page.locator(recent_selector).first).to_have_class(re.compile(r'\bactive\b'))
        page.locator('#userInput').click()
        other = context.new_page()
        other.goto(live_server, wait_until='domcontentloaded')
        other.evaluate("localStorage.setItem('tofu_recent_models', '[\"beta\",\"recent-test-0\"]')")
        page.bring_to_front()
        page.locator('#presetToggle').click()
        expect(page.locator(recent_selector).first).to_have_attribute('data-value', 'beta')
        page.reload(wait_until='domcontentloaded')
        expect(page.locator(recent_selector).first).to_have_attribute('data-value', 'beta')
        assert not errors, errors
    finally:
        context.close()

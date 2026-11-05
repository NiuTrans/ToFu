"""Real current-build journey: a template-added Provider card renders at once.

The repository publishes ``model_routing.changed`` inside its commit, before
the create endpoint returns, so Settings live sync is usually already reading
when the receipt arrives. Holding the receipt and each routing read pins that
production order; resource timing proves the wake-hint read was in flight.
"""
import time
from urllib.parse import urlparse

import pytest

from tests.e2e.test_e2e_smoke import _disable_open_mode_rate_limit, _wait_app_ready  # noqa: F401

pytestmark = pytest.mark.visual

TEMPLATE = 'deepseek'
# Lets the commit's wake hint reach the page while its receipt is still held.
HOLD_SECONDS = 0.5


def _payload(response):
    assert response.ok, response.text()
    body = response.json()
    return body.get('data', body)


def _remove_provider(page, base, provider_id):
    routing = _payload(page.request.get(f'{base}/api/v4/model-routing'))
    if any(row['provider_id'] == provider_id
           for row in routing['model_routing'].get('providers', [])):
        _payload(page.request.delete(f'{base}/api/v4/providers/{provider_id}',
                                     data={'expected_revision': routing['revision']}))


def _held(method):
    def hold(route):
        if route.request.method != method:
            route.continue_()
            return
        response = route.fetch()
        time.sleep(HOLD_SECONDS)
        route.fulfill(response=response)
    return hold


def test_template_card_appears_while_the_commit_wake_hint_read_is_in_flight(page, assert_no_js_errors):
    _wait_app_ready(page)
    base = page.url.rstrip('/')
    compiled = _payload(page.request.post(f'{base}/api/v4/providers/templates/compile',
                                          data={'template_key': TEMPLATE}))
    provider_id = compiled['provider_bundle']['provider']['provider_id']
    _remove_provider(page, base, provider_id)

    def is_create(url):
        return urlparse(url).path == '/api/v4/providers'

    def is_read(url):
        return urlparse(url).path == '/api/v4/model-routing'

    hold_create, hold_read = _held('POST'), _held('GET')
    try:
        page.locator('[data-tofu-action="openSettings()"]').first.click()
        page.wait_for_selector('#settingsModal.open', timeout=10000)
        page.evaluate("async () => { await window.TofuModules.resolveAction('switchSettingsTab')('api'); }")
        page.wait_for_selector(
            '#stgProviderList :is(details.stg-provider-card-v2, p.stg-empty)', timeout=15000)
        page.route(is_create, hold_create)
        page.route(is_read, hold_read)
        page.evaluate('() => performance.clearResourceTimings()')
        page.locator('[data-tofu-action="_showTemplateMenu(this)"]').click()
        page.locator(f'[data-tpl-key="{TEMPLATE}"]').click()

        card = page.locator(
            f'#stgProviderList details.stg-provider-card-v2[data-provider-id="{provider_id}"]')
        card.wait_for(state='visible', timeout=10000)
        page.wait_for_function(
            '(id) => document.querySelector(`#stgProviderList details[data-provider-id="${id}"]`)?.open',
            arg=provider_id, timeout=10000)
        timing = page.evaluate('''() => performance.getEntriesByType('resource').map(entry => ({
            path: new URL(entry.name).pathname, start: entry.startTime, end: entry.responseEnd}))''')
        receipt = next(row for row in timing if row['path'] == '/api/v4/providers')
        assert any(row['path'] == '/api/v4/model-routing' and row['start'] < receipt['end']
                   for row in timing), timing
        saved_but_stale = page.evaluate('''() => [
            'The operation was saved, but the view could not fully update',
            '操作已保存，但界面未能完整更新'].some(text => document.body.innerText.includes(text))''')
        assert not saved_but_stale
    finally:
        page.unroute(is_create, hold_create)
        page.unroute(is_read, hold_read)
        _remove_provider(page, base, provider_id)

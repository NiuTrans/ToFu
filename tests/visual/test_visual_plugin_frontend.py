"""The built host entry starts a real plugin page with explicit dependencies."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests._http_asset_origin import serve_asset_responses
from tests.frontend.test_plugin_frontend_delivery import plugin_page_app as plugin_page_app


pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def plugin_origin(plugin_page_app):
    async def capture():
        client = plugin_page_app.test_client()
        paths = ['/trading.html']
        manifest = json.loads((ROOT / 'static/vite/manifest.json').read_text())
        assets = set()
        for row in manifest.values():
            assets.add(row['file'])
            assets.update(row.get('css', []))
            assets.update(row.get('assets', []))
        paths += [f'/static/vite/{path}' for path in sorted(assets)]
        static = ROOT / 'plugins/tofu-trading/tofu_trading/static'
        paths += [f'/trading-static/{path.relative_to(static).as_posix()}'
                  for path in static.rglob('*') if path.is_file()]
        responses = {}
        for path in paths:
            response = await client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            value = (await response.get_data(), dict(response.headers))
            responses[path] = value
            responses['/proxy/15000' + path] = value
        return responses

    # Playwright owns the calling thread's event loop. Capture the native app
    # responses on a joined worker before starting browser navigation.
    with ThreadPoolExecutor() as worker:
        responses = worker.submit(lambda: asyncio.run(capture())).result()
    with serve_asset_responses(responses) as origin:
        yield origin


@pytest.mark.parametrize('prefix', ['', '/proxy/15000'])
def test_trading_page_starts_with_shared_api_markdown_and_fonts(browser, plugin_origin, prefix, tmp_path):
    page = browser.new_page()
    errors = []
    requests = []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def api(route):
        requests.append(route.request.url)
        route.fulfill(json={'data': {
            'holdings': [], 'available_cash': 0, 'sessions': [], 'tasks': [],
            'actions': [], 'targets': [], 'adoption': [], 'cycles': [],
        }, 'meta': {'requestId': 'plugin-browser'}})

    page.route('**/api/v4/trading/**', api)
    try:
        page.goto(plugin_origin + prefix + '/trading.html')
        expect(page.locator('body')).to_have_attribute('data-tofu-plugin-state', 'ready')
        expect(page.locator('#page-overview')).to_have_class('page active')
        page.get_by_role('button', name='今日对账', exact=True).click()
        expect(page.locator('#page-reconcile')).to_have_class('page active')
        expect(page.locator('#recPlanBody')).not_to_be_empty()
        assert requests and all(url.startswith(plugin_origin + prefix + '/api/v4/trading/') for url in requests)
        value = page.evaluate('''() => {
            const probe = document.createElement('div');
            probe.innerHTML = TradingApp.renderMarkdown('**Readable**\\n\\n<img src=x onerror="alert(1)"><script>alert(1)</script>');
            return {bold: probe.querySelector('strong')?.textContent,
                executable: !!probe.querySelector('script,[onerror]'),
                ambientApi: typeof window.Api};
        }''')
        assert value == {'bold': 'Readable', 'executable': False, 'ambientApi': 'undefined'}
        assert page.evaluate('''async () => {
            await document.fonts.load('16px Inter');
            await document.fonts.ready;
            return document.fonts.check('16px Inter') && ![...document.fonts].some(font => font.status === 'error');
        }''')
        assert errors == []
        screenshot = tmp_path / ('plugin-prefixed.png' if prefix else 'plugin-root.png')
        page.screenshot(path=str(screenshot), full_page=True)
        print(f'plugin screenshot: {screenshot}')
    finally:
        page.close()


def test_plugin_module_failure_shows_a_visible_error(browser, plugin_origin):
    page = browser.new_page()
    page.route('**/trading-static/js/trading/entry.js', lambda route: route.abort())
    try:
        page.goto(plugin_origin + '/trading.html')
        expect(page.locator('body')).to_have_attribute('data-tofu-plugin-state', 'error')
        expect(page.get_by_role('alert')).to_be_visible()
    finally:
        page.close()

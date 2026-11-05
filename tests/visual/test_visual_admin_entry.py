"""The independent admin bundle preserves its real boot and access presentation."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from lib.app.app_factory import create_base_app
from lib.static_serving import load_static_bytes, register_static_route
from tests._http_asset_origin import serve_asset_responses

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def admin_origin():
    from routes.common import common_bp

    app = create_base_app('admin-entry', {'TESTING': True})
    app.register_blueprint(common_bp)

    async def offload(loop, filename):
        return await loop.run_in_executor(None, load_static_bytes, str(ROOT / 'static'), filename)

    register_static_route(app, offload=offload, timeout=10, logger=logging.getLogger(__name__))

    async def capture():
        client = app.test_client()
        manifest = json.loads((ROOT / 'static/vite/manifest.json').read_text())
        assets = set()
        for row in manifest.values():
            assets.add(row['file'])
            assets.update(row.get('css', []))
            assets.update(row.get('assets', []))
        responses = {}
        for path in ['/admin', *[f'/static/vite/{asset}' for asset in sorted(assets)]]:
            response = await client.get(path)
            assert response.status_code == 200, (path, response.status_code)
            responses[path] = (await response.get_data(), dict(response.headers))
        return responses

    with ThreadPoolExecutor() as worker:
        responses = worker.submit(lambda: asyncio.run(capture())).result()
    with serve_asset_responses(responses) as origin:
        yield origin


@pytest.mark.parametrize('allowed', [False, True])
def test_admin_entry_boot_preserves_scope_and_capability_presentation(browser, admin_origin, allowed):
    page = browser.new_page()
    errors = []
    calls = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.add_init_script('''
        window.adminEntryReady = false;
        window.addEventListener('tofu:app-ready', event => {
            window.adminEntryReady = event.detail.entry === 'admin';
        });
        localStorage.setItem('tofu_ui_lang', 'en');
    ''')

    def api(route):
        path = urlsplit(route.request.url).path
        calls.append(path)
        payloads = {
            '/api/v4/auth/mode': {'mode': 'multi-user'},
            '/api/v4/users/me': {'principal': {'scopes': ['admin'] if allowed else []}},
            '/api/v4/capabilities': {'relay': {'billing_enabled': False, 'model_relay_enabled': False}},
            '/api/v4/users': {'users': []},
        }
        assert path in payloads, path
        route.fulfill(json={'data': payloads[path], 'meta': {'requestId': 'admin-browser'}})

    page.route('**/api/v4/**', api)
    try:
        page.goto(admin_origin + '/admin')
        page.wait_for_function('window.adminEntryReady === true')
        if allowed:
            expect(page.locator('#adminShell')).to_be_visible()
            expect(page.locator('#adminGate')).not_to_be_visible()
            expect(page.locator('#relayUsersBlock table')).to_be_visible()
            expect(page.locator('#agentOnlyBanner')).to_be_visible()
            for name in ['relayPricing', 'relayCodes', 'relayPayments']:
                expect(page.locator(f'.nav-tabs button[data-pane="{name}"]')).not_to_be_visible()
            assert '/api/v4/users' in calls
        else:
            expect(page.locator('#adminGate')).to_be_visible()
            expect(page.locator('#adminShell')).not_to_be_visible()
            assert calls == ['/api/v4/auth/mode', '/api/v4/users/me']
        assert errors == []
    finally:
        page.close()

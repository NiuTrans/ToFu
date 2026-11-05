"""Load both real MV3 packages in Chromium and exercise their popup contract."""
from pathlib import Path
import zipfile

import pytest
from playwright.sync_api import sync_playwright

from chromium_env import ensure_chromium_env
from tests.support.browser_extension_bundle import build_extension_bundle

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('store', [False, True], ids=['dev', 'store'])
def test_packaged_worker_registers_and_answers_popup(tmp_path, store):
    archive = build_extension_bundle(ROOT, tmp_path / 'checkout', store=store)
    extension = tmp_path / 'extension'
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(extension)
    ensure_chromium_env()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            str(tmp_path / 'profile'), channel='chromium', headless=True,
            args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
        )
        try:
            worker = (browser.service_workers[0] if browser.service_workers
                      else browser.wait_for_event('serviceworker', timeout=15000))
            extension_id = worker.url.split('/')[2]
            page = browser.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'chrome-extension://{extension_id}/popup.html')
            # Chrome storage callbacks finish after synchronous registration.
            # Wait for the public readiness signal, not an arbitrary sleep.
            page.wait_for_function(
                "() => document.querySelector('#clientIdText').dataset.full",
                timeout=10000,
            )
            status = page.evaluate("chrome.runtime.sendMessage({type: 'getStatus'})")
            assert status['inflight'] == status['resultQueue'] == 0
            assert status['commandsExecuted'] == status['commandsFailed'] == 0
            assert status['pollActive'] is False
            assert status['connected'] is False
            assert status['clientId'] == page.locator('#clientIdText').get_attribute('data-full')
            assert page.locator('#repairBtn').count() == 1
            for size in ('16', '48', '128'):
                image = page.locator('body').evaluate(
                    """async (body, size) => {
                        const icon = new Image();
                        icon.src = chrome.runtime.getURL(`icon${size}.png`);
                        await icon.decode();
                        body.append(icon);
                        return [icon.naturalWidth, icon.naturalHeight];
                    }""",
                    size,
                )
                assert image == [int(size), int(size)]
            assert errors == []
        finally:
            browser.close()

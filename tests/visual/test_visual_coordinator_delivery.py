"""Deliver the existing offline Coordinator through real owned artifact routes.

The imported server owns isolated storage/auth identity and exact production
panel/HTTP implementations. No prototype regeneration or application restart.
"""
from pathlib import Path

import pytest

from tests.visual.test_visual_rich_delivery import delivery_server as delivery_server

pytestmark = pytest.mark.visual
pytest_plugins = ('tests._artifact_sidecar',)
ROOT = Path(__file__).resolve().parents[2]


def test_actual_coordinator_snapshot_preview_and_explicit_interaction(browser, delivery_server, tmp_path):
    origin, _, _, _, _, _, _, receipt, source_bytes = delivery_server
    source = ROOT / 'designs/sidebar-coordinator/preview.html'
    assert source_bytes == source.read_bytes()
    assert receipt['sizeBytes'] > 3 * 1024 * 1024
    page = browser.new_page(viewport={'width': 1180, 'height': 1000}, accept_downloads=True)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.context.add_cookies([{'name': 'delivery-owner', 'value': '7', 'url': origin}])
    page.goto(origin + '/', wait_until='networkidle')
    # Initialize the production panel, then exercise its real published-ID
    # action delegation (the same intent emitted by native artifact chips).
    page.locator('#open-0').click()
    panel = page.locator('#artifact-panel')
    panel.locator('iframe').wait_for(state='attached')
    page.evaluate('''id => {
        const trigger = document.createElement('span');
        trigger.dataset.conversationAction = 'open-artifact';
        trigger.dataset.operation = id;
        trigger.id = 'coordinator-delivery-intent';
        trigger.textContent = 'Open delivered Coordinator';
        document.querySelector('#artifact-panel').append(trigger);
    }''', receipt['id'])
    page.locator('#coordinator-delivery-intent').click()
    iframe = panel.locator('iframe')
    page.wait_for_function('(url) => document.querySelector("#artifact-panel iframe")?.src.endsWith(url)',
                           arg=f"/api/artifacts/{receipt['id']}/view")
    frame = panel.frame_locator('iframe')
    assert iframe.get_attribute('sandbox') == 'allow-scripts'
    # /view permits only the server's trusted math bootstrap, not document code.
    assert frame.locator('body').evaluate('(node) => node.ownerDocument.defaultView.__prototypeReady !== true')
    with page.expect_download() as pending:
        panel.locator('[data-art-action=download]').click()
    download = pending.value
    assert download.suggested_filename.endswith('.txt')
    assert Path(download.path()).read_bytes() == source.read_bytes()
    panel.get_by_role('button', name='Run scripts (code / network risk)', exact=True).click()
    page.wait_for_function('''() => {
        const frame = document.querySelector('#artifact-panel iframe');
        return frame && frame.getAttribute('sandbox') === 'allow-scripts';
    }''')
    frame.locator('#conversation-title').wait_for(state='visible')
    assert frame.locator('#conversation-title').text_content() == 'Coordinator'
    frame.locator('#task-research').click()
    frame.locator('#back-coordinator').wait_for(state='visible')
    frame.locator('#back-coordinator').click()
    assert frame.locator('#conversation-title').text_content() == 'Coordinator'
    assert page.locator('#conversation-title').count() == 0
    page.screenshot(path=str(tmp_path / 'coordinator-published-delivery.png'), full_page=True)
    panel.locator('[data-art-action=close]').click()
    assert panel.locator('iframe').count() == 0
    assert not errors, errors
    page.close()

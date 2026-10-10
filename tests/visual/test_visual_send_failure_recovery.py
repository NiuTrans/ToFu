"""Click and Enter expose an unavailable backend and allow an explicit retry."""
import json

import pytest

pytestmark = pytest.mark.visual


def test_click_and_enter_restore_draft_and_show_backend_failure(page, screenshot_dir):
    page.wait_for_function('typeof newChat === "function"')
    page.evaluate('newChat()')
    requests = []
    def reject(route):
        if route.request.method != 'POST':
            route.continue_()
            return
        requests.append(route.request.post_data_json)
        route.fulfill(status=503, content_type='application/json', body=json.dumps({
            'ok': False, 'error': {'code': 'database_unavailable',
                'message': 'Storage temporarily unavailable; retry your saved draft.'},
        }))
    page.route('**/api/v4/conversations/*/turns', reject)
    textarea = page.locator('#userInput')
    textarea.fill('Retain this message when storage is unavailable')
    page.locator('#sendBtn').click()
    page.wait_for_function('''() => document.querySelector('#toastContainer')
        ?.textContent.includes('Storage temporarily unavailable')''', timeout=30000)
    assert textarea.input_value() == 'Retain this message when storage is unavailable'
    assert requests
    command_id = requests[0]['commandId']
    assert all(request['commandId'] == command_id for request in requests)
    first_attempt_count = len(requests)
    with page.expect_request(lambda request: request.method == 'POST'
            and request.url.endswith('/turns')):
        textarea.press('Enter')
    # Wait on the observable request/draft boundary, not a rendering delay.
    from playwright.sync_api import expect
    expect(textarea).to_have_value('Retain this message when storage is unavailable', timeout=30000)
    assert len(requests) > first_attempt_count
    assert all(request['commandId'] == command_id for request in requests)
    page.screenshot(path=str(__import__('pathlib').Path(screenshot_dir) / 'send-failure-recovery.png'))

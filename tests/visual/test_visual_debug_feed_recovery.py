"""The shipped Debug drawer retains complete evidence through transient reads.

This journey opens the production lazy diagnostics bundle in a real browser on
an isolated test server. Only the wire-read endpoint is intercepted; no
conversation or persistent user data is created.
"""

import json

import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


def test_debug_feed_partial_read_keeps_messages_and_retry_recovers(page, assert_no_js_errors):
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready

    _wait_ready(page)
    turn = {'type': 'turn', 'msgIndex': 0, 'turnId': 't-visual',
            'taskId': 'task-visual', 'createdAt': 1, 'status': 'done'}
    nav = {'turnId': 't-visual', 'taskId': 'task-visual',
           'msgIndex': 0, 'createdAt': 1, 'status': 'done'}
    good = {'messages': [{'role': 'user', 'content': 'browser evidence'}],
            'sections': [turn], 'turns': [nav]}
    partial = {'messages': [], 'sections': [{**turn, 'readError': True}],
               'turns': [{**nav, 'readError': True}], 'readError': True}
    replies = [good, partial, good]

    def respond(route):
        payload = replies.pop(0) if replies else good
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps(payload))

    page.route('**/api/v4/tasks/conv-wire/*', respond)
    # The debug shell reads its conversation from the normal active selection.
    # A synthetic ID is safe here: the endpoint is intercepted, never written.
    page.evaluate("window.activeConvId = 'visual-debug-feed'")
    _invoke_action(page, 'toggleDebug')
    page.wait_for_selector('#debugContent .debug-msg-block')
    assert 'Messages (1)' in page.locator('#debugTitle').inner_text()
    _invoke_action(page, 'riRefreshTasks')
    page.wait_for_selector('#debugContent .ri-warn-line .ri-retry')
    assert page.locator('#debugContent .debug-msg-block').count() == 1
    assert 'Messages (1)' in page.locator('#debugTitle').inner_text()
    # The keyless test server may auto-open first-run onboarding after the
    # page fixture's initial dismissal; reproduce a user's close before click.
    from tests.conftest import _dismiss_onboarding_modals
    _dismiss_onboarding_modals(page)
    page.locator('#debugContent .ri-warn-line .ri-retry').click()
    page.wait_for_function("!document.querySelector('#debugContent .ri-warn-line')")
    assert page.locator('#debugContent .debug-msg-block').count() == 1

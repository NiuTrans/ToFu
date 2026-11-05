"""Actual Inspector controls preserve selected evidence across late responses."""
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('scenario', ['trace_reopen', 'detached_order', 'detached_retry_back'])
def test_inspector_read_controls(browser, live_server, scenario):
    from playwright.sync_api import expect
    from tests.visual.test_visual_surfaces import _invoke_action

    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    pending, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    turn = {'type': 'turn', 'msgIndex': 0, 'turnId': 'synthetic-turn', 'taskId': 'synthetic-task', 'createdAt': 1, 'status': 'done'}
    wire = {'messages': [{'role': 'user', 'content': 'synthetic feed evidence'}],
            'sections': [turn], 'turns': [{key: value for key, value in turn.items() if key != 'type'}]}

    def data(route, body):
        return route.fulfill(json={'data': body, 'meta': {}})

    def handle(route):
        parsed = urlparse(route.request.url)
        if parsed.netloc != urlparse(live_server).netloc:
            return route.abort()
        if parsed.path.startswith('/api/v4/tasks/conv-wire/'):
            return data(route, wire)
        if parsed.path.startswith('/api/v4/tasks/') and ('/requests/' in parsed.path or parsed.path.endswith('/trace')):
            pending.append(route)
            return
        if '/chat' in parsed.path and route.request.method == 'POST':
            raise AssertionError('Unexpected model request')
        return route.continue_()

    def open_detail(task):
        with page.expect_request(lambda request: '/requests/' in urlparse(request.url).path):
            page.evaluate("task => { void window.TofuModules.invokeFeature('openToolDebugPanel', [task, 1], () => {}); }", task)
        expect(page.locator('#debugContent .ri-feed-back button')).to_be_visible()

    def settle(route, body):
        with page.expect_response(lambda response: response.url == route.request.url):
            data(route, body)
        # Fetch JSON and its current-view publication settle before the next paint.
        page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')

    def trace(label):
        return {'eventsAvailable': True, 'tStart': 1000, 'totalMs': 1000, 'summary': {'llmMs': 1000},
                'spans': [{'kind': 'llm', 'name': label, 'depth': 0, 'tStart': 1000, 'tEnd': 2000}],
                'gaps': [], 'statusHistory': [], 'clientObservations': [], 'running': False}

    def reveal_message(text):
        header = page.locator('#debugContent .debug-msg-header').first
        expect(header).to_be_visible()
        if header.get_attribute('aria-expanded') != 'true':
            header.click()
        expect(page.locator('#debugContent')).to_contain_text(text)

    context.route('**/*', handle)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.evaluate("window.activeConvId = 'synthetic-inspector'")
        _invoke_action(page, 'toggleDebug')
        reveal_message('synthetic feed evidence')
        if scenario == 'trace_reopen':
            page.locator('#debugContent .ri-feed-trace').click()
            expect(page.locator('#debugContent .ri-feed-back button')).to_be_visible()
            page.locator('#riDrawer [data-tofu-action="closeDebug()"]:visible').first.click()
            _invoke_action(page, 'toggleDebug')
            page.locator('#debugContent .ri-feed-trace').click()
            expect(page.locator('#debugContent .ri-feed-back button')).to_be_visible()
            settle(pending.pop(1), trace('current trace'))
            expect(page.locator('#debugContent')).to_contain_text('current trace')
            settle(pending.pop(0), trace('stale trace'))
            expect(page.locator('#debugContent')).to_contain_text('current trace')
            expect(page.locator('#debugContent')).not_to_contain_text('stale trace')
        elif scenario == 'detached_order':
            open_detail('first')
            open_detail('second')
            settle(pending.pop(1), {'messages': [{'role': 'assistant', 'content': 'current detail'}]})
            reveal_message('current detail')
            settle(pending.pop(0), {'messages': [{'role': 'assistant', 'content': 'stale detail'}]})
            reveal_message('current detail')
            expect(page.locator('#debugContent')).not_to_contain_text('stale detail')
        else:
            open_detail('retry-task')
            pending.pop(0).fulfill(status=503, content_type='application/problem+json',
                json={'type': 'about:blank', 'title': 'Unavailable', 'status': 503, 'code': 'synthetic_failure'})
            retry = page.locator('#debugContent').get_by_role('button', name='Retry', exact=True)
            expect(retry).to_be_visible()
            retry.click()
            expect(page.locator('#debugContent .ri-loading')).to_be_visible()
            page.locator('#debugContent .ri-feed-back button').click()
            reveal_message('synthetic feed evidence')
            settle(pending.pop(0), {'messages': [{'role': 'assistant', 'content': 'retired retry'}]})
            reveal_message('synthetic feed evidence')
            expect(page.locator('#debugContent')).not_to_contain_text('retired retry')
        assert not errors, errors
    finally:
        for route in pending:
            route.abort()
        context.close()

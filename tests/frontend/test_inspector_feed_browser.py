"""Selected evidence survives feed failure; shared controls fit the actual drawer."""
import pytest

pytestmark = pytest.mark.visual

CASES = [('failure_behind_detail', 'light', 1180), ('back_pending', 'light', 1180),
         ('retry_keyboard', 'light', 1180)] + [
             ('reading', theme, width) for theme in ('light', 'dark', 'tofu') for width in (390, 960, 1180)
         ]


@pytest.mark.parametrize('scenario,theme,width', CASES)
def test_inspector_feed_controls(browser, live_server, tmp_path, scenario, theme, width):
    from playwright.sync_api import expect
    from tests.visual.test_visual_surfaces import _invoke_action

    context = browser.new_context(service_workers='block')
    context.add_init_script("localStorage.setItem('tofu_ui_lang','en');localStorage.setItem('tofu_onboarding_v1_done','1');")
    page = context.new_page()
    page.set_viewport_size({'width': width, 'height': 1000})
    pending, errors = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    context.route('**/api/v4/tasks/conv-wire/inspector-feed-browser', lambda route: pending.append(route))
    context.route('**/api/v4/tasks/inspector-task/requests/**', lambda route: route.fulfill(json={
        'data': {'messages': [{'role': 'tool', 'content': 'Selected tool evidence stays visible'}]}, 'meta': {}}))
    wire = {'messages': [{'role': 'assistant', 'content': 'The request and tool evidence are available.',
                         'tool_calls': [{'id': 'read', 'type': 'function', 'function': {
                             'name': 'read_files', 'arguments': '{"paths":["src/handler.ts"]}'}}]}],
            'sections': [{'type': 'turn', 'msgIndex': 0, 'turnId': 'turn', 'taskId': 'inspector-task',
                          'status': 'running', 'live': True, 'createdAt': 1, 'preview': 'Inspect the handler and its failure contract'}],
            'turns': [{'turnId': 'turn', 'taskId': 'inspector-task', 'status': 'running', 'live': True}]}

    def settle(value):
        assert pending, 'Expected the isolated feed request to be held'
        pending.pop(0).fulfill(json={'data': value, 'meta': {}})

    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
        page.evaluate('(theme) => {document.documentElement.dataset.theme=theme;window.activeConvId="inspector-feed-browser";}', theme)
        if scenario in ('failure_behind_detail', 'back_pending'):
            with page.expect_request(lambda request: '/conv-wire/inspector-feed-browser' in request.url):
                _invoke_action(page, 'openToolDebugPanel', 'inspector-task', 1)
            content = page.locator('#debugContent')
            content.locator('.debug-msg-header').first.click()
            expect(content).to_contain_text('Selected tool evidence stays visible')
            if scenario == 'failure_behind_detail':
                with page.expect_response(lambda response: '/conv-wire/inspector-feed-browser' in response.url):
                    settle(None)
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                expect(content).to_contain_text('Selected tool evidence stays visible')
                content.locator('.ri-feed-back button').click()
                expect(content.locator('.ri-main-empty .ri-retry')).to_be_visible()
            else:
                content.locator('.ri-feed-back button').click()
                expect(content.locator('.ri-loading')).to_be_visible()
                settle(wire)
                expect(content.locator('.debug-msg-block')).to_have_count(1)
        else:
            with page.expect_request(lambda request: '/conv-wire/inspector-feed-browser' in request.url):
                _invoke_action(page, 'toggleDebug')
            content = page.locator('#debugContent')
            expect(content.locator('.ri-loading')).to_be_visible()
            if scenario == 'retry_keyboard':
                settle(None)
                retry = content.locator('.ri-retry')
                expect(retry).to_be_visible()
                retry.focus()
                with page.expect_request(lambda request: '/conv-wire/inspector-feed-browser' in request.url):
                    retry.press('Enter')
                expect(content.locator('.ri-loading')).to_be_visible()
                assert content.evaluate('node => node._rawMessages === null && node._rawTools === null')
                settle(wire)
                expect(content.locator('.debug-msg-block')).to_have_count(1)
            else:
                settle({**wire, 'readError': True, 'eventReadBounds': [{'reason': 'event-limit'}], 'olderTurnsDropped': 3})
                expect(content.locator('.ri-warn-line .ri-retry')).to_be_visible()
                expect(content.locator('.ri-feed-truncated')).to_have_count(2)
                content.locator('.debug-msg-header').first.click()
                controls = content.locator('.ri-retry, .ri-turn-chip, .ri-feed-trace')
                assert controls.count() == 3
                for control in controls.all():
                    expect(control).to_have_attribute('type', 'button')
                    bounds = control.evaluate('node => {const r=node.getBoundingClientRect();return {left:r.left,right:r.right,height:r.height,width:r.width,viewport:document.documentElement.clientWidth};}')
                    assert bounds['height'] >= 28 and bounds['width'] >= 28, bounds
                    assert bounds['left'] >= 0 and bounds['right'] <= bounds['viewport'] + 1, bounds
                preview = content.locator('.ri-feed-turn-preview').evaluate('node => ({width:node.clientWidth,font:parseFloat(getComputedStyle(node).fontSize)})')
                assert preview['width'] >= preview['font'] * 8, 'Reply preview must show readable context beside native controls'
                content.locator('.ri-retry').focus()
                page.locator('#riDrawer').screenshot(path=str(tmp_path / f'inspector-feed-{theme}-{width}.png'))
        assert not errors, errors
    finally:
        context.close()

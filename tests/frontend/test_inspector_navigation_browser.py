"""Native Inspector navigation remains usable during refresh and on narrow screens."""
import pytest

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [390, 1180])
def test_inspector_navigation_controls(page, assert_no_js_errors, tmp_path, width):
    from playwright.sync_api import expect
    from tests.visual.test_visual_surfaces import _invoke_action, _wait_ready
    from tests.conftest import _dismiss_onboarding_modals

    page.set_viewport_size({'width': width, 'height': 1000})
    page.evaluate("localStorage.setItem('tofu_ui_lang', 'en')")
    page.reload(wait_until='domcontentloaded')
    _wait_ready(page)
    ids = ['one', 'two', 'three']
    revision = 0

    def wire(route):
        rows = [{'turnId': name, 'taskId': 'task-' + name, 'createdAt': i + 1,
                 'preview': f'reply {name} revision {revision}'} for i, name in enumerate(ids)]
        route.fulfill(json={'data': {
            'messages': [{'role': 'user', 'content': 'evidence ' + name} for name in ids],
            'turns': rows, 'sections': [dict(row, type='turn', msgIndex=i) for i, row in enumerate(rows)],
        }, 'meta': {}})

    def refresh():
        with page.expect_response(lambda response: '/api/v4/tasks/conv-wire/' in response.url):
            _invoke_action(page, 'riRefreshTasks')

    page.route('**/api/v4/tasks/conv-wire/*', wire)
    page.evaluate("window.activeConvId = 'navigation-controls'")
    _invoke_action(page, 'toggleDebug')
    _dismiss_onboarding_modals(page)
    page.evaluate("document.documentElement.dataset.theme = 'tofu'")
    expect(page.locator('#debugContent .ri-feed-turn')).to_have_count(3)
    anchor, panel = page.locator('#riTurnPos'), page.locator('#riTurnJump')
    anchor.click()
    expect(panel).to_be_visible()
    bounds = panel.bounding_box()
    assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1
    drawer = page.locator('#riDrawer').bounding_box()
    assert drawer and bounds['x'] >= drawer['x']
    assert bounds['x'] + bounds['width'] <= drawer['x'] + drawer['width'] + 1
    middle = panel.locator('.ri-turn-jump-row').filter(has_text='reply two')
    middle.focus()
    revision = 1
    refresh()
    expect(middle).to_contain_text('revision 1')
    expect(middle).to_be_focused()
    page.screenshot(path=str(tmp_path / f'inspector-navigation-tofu-{width}.png'))
    middle.press('Enter')
    expect(panel).to_be_hidden()
    expect(page.locator('#debugContent [data-turn-id="two"]')).to_have_class('ri-feed-turn ri-flash')
    expect(anchor).to_be_focused()
    anchor.click()
    middle.focus()
    ids.remove('two')
    refresh()
    newest = panel.locator('.ri-turn-jump-row').filter(has_text='reply three')
    expect(panel.locator('.ri-turn-jump-row')).to_have_count(2)
    expect(newest).to_be_focused()
    newest.press('Escape')
    expect(panel).to_be_hidden()
    expect(anchor).to_be_focused()
    anchor.click()
    page.locator('#riDrawer [data-tofu-action="closeDebug()"]:visible').first.click()
    expect(panel).to_be_hidden()
    expect(anchor).to_have_attribute('aria-expanded', 'false')

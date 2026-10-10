"""Shipped composer model selection: native keyboard controls, fresh provider preference and bounded popup geometry."""
import pytest

pytestmark = [pytest.mark.visual, pytest.mark.slow]


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
@pytest.mark.parametrize('width,height', [(390, 844), (1180, 1000)])
def test_model_picker_provider_preference_and_keyboard(page, assert_no_js_errors, tmp_path, width, height, theme):
    from tests.model_routing.test_model_picker_startup_browser import _picker_catalog
    from lib.model_routing.controls import reasoning_controls
    from tests.visual.test_visual_surfaces import _wait_ready
    from tests.conftest import _dismiss_onboarding_modals

    catalog = _picker_catalog()
    page.set_viewport_size({'width': width, 'height': height})
    page.route('**/api/v4/model-routing', lambda route: route.fulfill(json={
        'ok': True, 'revision': catalog['revision'], 'model_routing': catalog,
        'reasoning_controls': reasoning_controls(catalog)}))
    page.reload(wait_until='domcontentloaded')
    _wait_ready(page)
    _dismiss_onboarding_modals(page)
    page.evaluate('theme => { document.documentElement.dataset.theme = theme; }', theme)
    page.locator('#presetToggle').click()
    row = page.locator('#presetDropdownList .preset-dropdown-item[data-value="alpha"]').first
    choice = row.locator('.ps-dd-model-choice')
    choice.focus()
    choice.press('Enter')
    page.locator('#presetToggle').click()
    chip = row.locator('.ps-dd-provider-chip')
    chip.focus()
    chip.press('Enter')
    menu = page.locator('#psProviderMenu')
    menu.wait_for(state='visible')
    provider_b = menu.locator('button', has_text='Provider B')
    provider_b.click()
    assert row.locator('.ps-dd-provider-chip-label').inner_text() == 'Provider B'
    # Reopening must use the updated preference, not the row's old closure.
    chip.focus()
    chip.press('Enter')
    selected = menu.locator('[aria-selected="true"]')
    assert selected.inner_text() == 'Provider B'
    selected.press('Home')
    assert menu.locator('button').first.evaluate('(button) => button === document.activeElement')
    menu.evaluate('(element) => Promise.all(element.getAnimations({subtree:true}).map(animation => animation.finished.catch(() => {})))')
    page.screenshot(path=str(tmp_path / f'model-picker-provider-{theme}-{width}.png'))
    bounds = menu.bounding_box()
    assert bounds['x'] >= 0 and bounds['y'] >= 0
    assert bounds['x'] + bounds['width'] <= width + 1
    assert bounds['y'] + bounds['height'] <= height + 1
    menu.locator('button').first.press('Escape')
    assert not menu.count()
    assert chip.evaluate('(button) => button === document.activeElement')
    assert page.locator('#thinkingDepthSection').count() == 1


def test_long_provider_caption_stays_inside_its_popup(page, assert_no_js_errors):
    from tests.model_routing.test_model_picker_startup_browser import _picker_catalog
    from lib.model_routing.controls import reasoning_controls
    from tests.visual.test_visual_surfaces import _wait_ready
    from tests.conftest import _dismiss_onboarding_modals
    catalog = _picker_catalog()
    name = 'Provider B ' + ('long organization name ' * 10).rstrip()
    catalog['providers'][1]['name'] = name
    controls = reasoning_controls(catalog)
    page.set_viewport_size({'width': 390, 'height': 844})
    page.route('**/api/v4/model-routing', lambda route: route.fulfill(json={'ok': True, 'model_routing': catalog,
               'reasoning_controls': controls}))
    page.reload(wait_until='domcontentloaded')
    _wait_ready(page)
    _dismiss_onboarding_modals(page)
    page.locator('#presetToggle').click()
    page.locator('[data-value="alpha"] .ps-dd-provider-chip').click()
    menu = page.locator('#psProviderMenu')
    label = menu.locator('.ps-dd-provider-menu-label', has_text=name)
    label.wait_for(state='visible')
    assert label.get_attribute('title') == name
    assert label.evaluate('(element) => getComputedStyle(element).overflow') == 'hidden'
    bounds = label.bounding_box()
    popup = menu.bounding_box()
    assert bounds['x'] + bounds['width'] <= popup['x'] + popup['width'] + 1

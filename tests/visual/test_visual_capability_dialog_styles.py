"""Memory and Local Control styles follow their feature's admission and retry lifecycle."""
import pytest
from playwright.sync_api import expect

from tests.visual.test_visual_feature_style_residency import (
    _context, _invoke, _ready, _styles,
)

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]
CASES = [
    ('memory', 'openMemoryModal', 'closeMemoryModal', '#memoryModal', '.memory-modal'),
    ('local-control', 'openLocalControlModal', 'closeLocalControlModal', '#localControlModal', '.local-modal'),
]


@pytest.mark.parametrize('case', CASES, ids=['memory', 'local-control'])
@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
@pytest.mark.parametrize('width', [390, 1280])
def test_dialog_waits_for_its_styles_and_reuses_them(browser, live_server, case, theme, width):
    feature, open_action, close_action, overlay, dialog = case
    styles = _styles(f'frontend/src/features/{feature}.ts')
    context = _context(browser, width)
    page = context.new_page()
    held, requested, errors = [], [], []
    page.on('request', lambda request: requested.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    for asset in styles:
        page.route('**/' + asset, lambda route: held.append(route))
    matching = lambda: [url for url in requested if any(url.endswith('/' + asset) for asset in styles)]
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _ready(page)
        page.evaluate('theme => { document.documentElement.dataset.theme=theme; document.body.dataset.theme=theme; }', theme)
        assert not matching(), requested
        with page.expect_request(lambda request: any(request.url.endswith('/' + asset) for asset in styles)):
            _invoke(page, open_action, wait=False)
        page.wait_for_function('assets => assets.every(asset => [...document.querySelectorAll("link")].some(link => link.href.endsWith("/"+asset)))', arg=list(styles))
        assert held, 'CSS admission must precede the feature becoming visible'
        expect(page.locator(overlay)).not_to_have_class('modal-overlay open')
        for route in held:
            route.continue_()
        held.clear()
        expect(page.locator(overlay + '.open')).to_be_visible()
        panel = page.locator(overlay + ' ' + dialog)
        # Measure the settled frame, not a scaled opening-animation frame.
        panel.evaluate('async element => { await Promise.all(element.getAnimations().map(animation => animation.finished)); }')
        bounds = panel.bounding_box()
        assert bounds and bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width
        if feature == 'memory' and width == 390:
            # Memory's frame must keep the shared mobile full-screen policy
            # even though its body styles arrive after the application sheet.
            assert bounds == {'x': 0, 'y': 0, 'width': width, 'height': page.viewport_size['height']}
        assert page.evaluate('assets => assets.every(asset => [...document.styleSheets].some(sheet => sheet.href?.endsWith("/"+asset) && sheet.cssRules.length))', list(styles))
        _invoke(page, close_action)
        _invoke(page, open_action)
        expect(page.locator(overlay + '.open')).to_be_visible()
        assert len(matching()) == len(styles), matching()
        assert not errors, errors
    finally:
        for route in held:
            route.abort()
        context.close()


@pytest.mark.parametrize('case', CASES, ids=['memory', 'local-control'])
def test_failed_dialog_styles_remain_closed_after_existing_reload_recovery(browser, live_server, case):
    feature, open_action, _, overlay, _ = case
    failed_asset = sorted(_styles(f'frontend/src/features/{feature}.ts'))[0]
    context = _context(browser)
    page = context.new_page()
    attempts, documents = [], []

    def fail(route):
        attempts.append(route.request.url)
        route.fulfill(status=503, content_type='text/css', body='Unavailable dialog stylesheet')

    page.route('**/' + failed_asset, fail)
    page.on('request', lambda request: documents.append(request.url) if request.is_navigation_request() else None)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _ready(page)
        _invoke(page, open_action, wait=False)
        page.wait_for_function("sessionStorage.getItem('tofu:feature-load-reload') !== null")
        page.wait_for_function("window.__testAppReady && sessionStorage.getItem('tofu:feature-load-pending') === null")
        result = page.evaluate('''async name => {
          try { await window.TofuModules.invokeFeature(name, [], () => {}); return 'Unexpected success'; }
          catch (error) { return error.message; }
        }''', open_action)
        assert result.startswith('Unable to preload CSS for '), result
        expect(page.locator(overlay)).not_to_have_class('modal-overlay open')
        assert len(attempts) == len(documents) == 2, (attempts, documents)
    finally:
        context.close()

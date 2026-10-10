"""Production slide editor CSS loads with its feature graph; no deck is written."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('theme', ['light', 'dark', 'tofu'])
def test_editor_styles_wait_for_lazy_entry_and_keep_control_geometry(browser, live_server, theme, tmp_path):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    from tests._runtime_sections import native_module_path
    page = browser.new_page(viewport={'width': 1280, 'height': 1000})
    requests, errors = [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    manifest = json.loads((ROOT / 'static/vite/manifest.json').read_text())
    entry = manifest['frontend/src/features/slides.ts']
    assert entry.get('css'), 'Editor stylesheet belongs to the demand-loaded graph'
    def slide_read(route):
        assert route.request.method == 'GET', 'This fixture must not change a slide'
        url = route.request.url.split('?')[0]
        if url.endswith('.html'):
            route.fulfill(status=200, content_type='text/html', body='<html><body><h1 data-element-id="title">Fixture slide</h1></body></html>')
        elif url.endswith('/elements'):
            route.fulfill(status=200, content_type='application/json', body=json.dumps({'elements':[
                {'elementId':'title','elementType':'text','text':'Fixture slide','style':{'fontSize':'32px','color':'#243d35'}}]}))
        else:
            route.fulfill(status=200, content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg" width="160" height="90"><rect width="160" height="90" fill="white"/></svg>')
    page.route('**/api/v4/slides/style-fixture/**', slide_read)
    try:
        page.goto(live_server)
        _dismiss_onboarding_modals(page)
        page.evaluate('(theme)=>document.documentElement.dataset.theme=theme', theme)
        assert not any(url.endswith('/' + css) for url in requests for css in entry['css'])
        assert '.sled-overlay' not in (ROOT / 'static/styles.css').read_text()
        # Use the registered delegated action rather than exposing private editor state.
        page.evaluate("""() => {
          const trigger=document.createElement('button');trigger.id='style-editor-trigger';
          trigger.setAttribute('data-tofu-action', "openSlidesEditor('style-fixture',1,2)");
          document.body.append(trigger);trigger.click();
        }""")
        expect(page.locator('.sled-overlay')).to_be_visible(timeout=20000)
        assert any(url.endswith('/' + css) for url in requests for css in entry['css'])
        assert page.locator('.sled-overlay').evaluate('(el)=>getComputedStyle(el).position') == 'fixed'
        assert page.locator('.sled-overlay').evaluate('(el)=>getComputedStyle(el).display') == 'flex'
        assert page.locator('.sled-shell').evaluate('(el)=>getComputedStyle(el).boxShadow') != 'none'
        expect(page.frame_locator('.sled-canvas-frame').locator('[data-element-id=title]')).to_be_visible()
        page.frame_locator('.sled-canvas-frame').locator('[data-element-id=title]').click()
        expect(page.locator('#sledText')).to_have_value('Fixture slide')
        assert page.locator('.sled-btn--primary').evaluate('(el)=>getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
        # The shared switch consumes the named one-pixel control inset in every theme.
        page.add_script_tag(path=native_module_path('.native/style-choice.js', ROOT / 'frontend/src/ui/choice.ts'))
        page.evaluate("document.body.append(createChoice({type:'switch',label:'Style geometry'}).el)")
        assert page.locator('input.ui-switch').evaluate("(el)=>[getComputedStyle(el,'::after').top,getComputedStyle(el,'::after').left]") == ['1px','1px']
        page.screenshot(path=str(tmp_path / f'lazy-editor-{theme}.png'))
        page.locator('.sled-close').click()
        expect(page.locator('.sled-overlay')).to_have_count(0)
        assert not errors
    finally:
        page.close()

"""Production browser graph boundaries and CSS-failure recovery; APIs are read-only fixtures."""
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def _manifest():
    return json.loads((ROOT / 'static/vite/manifest.json').read_text())


def _fixture_reads(page):
    def serve(route):
        assert route.request.method == 'GET', 'This journey must not submit backend work'
        tail = route.request.url.split('/api/v4/evolution/')[1].split('?')[0]
        payload = {'programs': [], 'runs': [], 'models': [], 'goals': [], 'plans': [],
                   'episodes': [], 'mutations': [], 'capability_gaps': [], 'skills': [],
                   'snapshot': {}, 'receipts': [], 'experiments': []}
        route.fulfill(status=200, content_type='application/json', body=json.dumps(payload))
    page.route('**/api/v4/evolution/**', serve)


def _open(page, origin):
    from tests.conftest import _dismiss_onboarding_modals
    from playwright.sync_api import expect
    _fixture_reads(page)
    page.goto(origin)
    _dismiss_onboarding_modals(page)
    page.locator('#evolutionBtn').click()
    expect(page.locator('.exp-create-form')).to_be_visible(timeout=20000)


def test_notebook_loads_no_studio_or_other_page_graph(browser, live_server):
    from playwright.sync_api import expect
    manifest = _manifest()
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    requests, errors = [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        _open(page, live_server)
        objective = page.locator('.exp-create-form [name=objective]')
        objective.fill('Retain the unsent notebook goal')
        page.evaluate("window.notebookForm=document.querySelector('.exp-create-form')")
        # Re-enter through the same delegated action used by intent replay;
        # the chat toolbar is hidden while this workspace is visible.
        page.evaluate("document.getElementById('evolutionBtn').click()")
        expect(objective).to_have_value('Retain the unsent notebook goal')
        assert page.evaluate("document.querySelector('.exp-create-form') === window.notebookForm")
        requested = lambda key: any(url.endswith('/' + manifest[key]['file']) for url in requests)
        assert requested('frontend/src/features/evolution/programs/page.ts')
        for key in ['studio-entry.ts', 'view.ts', 'research/page.ts']:
            assert not requested('frontend/src/features/evolution/' + key)
        assert page.evaluate("customElements.get('wa-drawer') === undefined")
        assert not page.locator('style[data-evolution-components]').count()
        page.evaluate("location.hash='/evolution/runs'")
        expect(page.locator('.evo-studio')).to_be_visible(timeout=20000)
        page.wait_for_function("customElements.get('wa-drawer') !== undefined")
        assert requested('frontend/src/features/evolution/studio-entry.ts')
        assert page.locator('style[data-evolution-components]').count() == 1
        page.evaluate("location.hash='/chat'")
        expect(page.locator('#userInput')).to_be_visible()
        assert page.locator('#evolutionPageContainer').evaluate('(root)=>root.childElementCount') == 0
        assert not errors
    finally:
        page.close()


@pytest.mark.parametrize('width', [390, 1440])
def test_studio_css_failure_stays_visible_and_reload_restores_graph(browser, live_server, width, tmp_path):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals
    manifest = _manifest()
    studio = manifest['frontend/src/features/evolution/studio-entry.ts']
    assert studio.get('css'), 'Studio entry must carry its own stylesheet transaction'
    page = browser.new_page(viewport={'width': width, 'height': 960})
    requests, errors = [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    # Exercise the visible fallback after the shared automatic recovery bound.
    page.add_init_script("sessionStorage.setItem('tofu:feature-load-reload', String(Date.now()))")
    css_patterns = ['**/' + css for css in studio['css']]
    for pattern in css_patterns:
        page.route(pattern, lambda route: route.abort('failed'))
    try:
        _open(page, live_server)
        page.evaluate("location.hash='/evolution/runs'")
        alert = page.locator('#evolutionPageContainer .ui-alert--error')
        expect(alert).to_be_visible(timeout=20000)
        assert not page.locator('.evo-studio').count()
        assert page.evaluate("customElements.get('wa-drawer') === undefined")
        assert page.locator('#evolutionPageContainer').get_attribute('aria-busy') is None
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(tmp_path / f'evolution-route-failure-{width}.png'))
        failed_requests = len([url for url in requests if any(url.endswith('/' + css) for css in studio['css'])])
        page.evaluate("location.hash='/evolution/programs'")
        expect(page.locator('.exp-create-form')).to_be_visible()
        page.evaluate("location.hash='/evolution/runs'")
        expect(alert).to_be_visible()
        assert len([url for url in requests if any(url.endswith('/' + css) for css in studio['css'])]) == failed_requests
        for pattern in css_patterns:
            page.unroute(pattern)
        page.locator('#evolutionPageContainer button').click()
        _dismiss_onboarding_modals(page)
        expect(page.locator('.evo-studio')).to_be_visible(timeout=20000)
        page.wait_for_function("customElements.get('wa-drawer') !== undefined")
        assert not errors
    finally:
        page.close()

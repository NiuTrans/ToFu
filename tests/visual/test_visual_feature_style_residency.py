"""Production CSS demand, before-visible loading and failed-load recovery.

Real local shell, Vite assets and feature ports; no provider calls. A fresh
context dismisses onboarding through its durable UI preference so startup has
no implicit settings intent. Budgets assert requests rather than host timing.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError

pytestmark = pytest.mark.visual
ROOT = Path(__file__).resolve().parents[2]


def _styles(entry: str) -> set[str]:
    manifest = json.loads((ROOT / 'static/vite/manifest.json').read_text())
    # Explicit manual chunks may merge the entry and omit its source-key row.
    rows = [manifest[entry]] if entry in manifest else [
        row for row in manifest.values()
        if row.get('name') == Path(entry).stem and row.get('isDynamicEntry')
    ]
    assert len(rows) == 1, f'Feature has no unique delivery owner: {entry}'
    styles = set(rows[0].get('css', []))
    assert styles, f'Feature lacks its declared CSS: {entry}'
    return styles


def _ready(page):
    page.wait_for_selector('#userInput', state='visible', timeout=20000)
    page.wait_for_function("window.TofuModules?.version === 3 && window.__testAppReady", timeout=20000)


def _invoke(page, name, *, wait=True):
    source = "window.TofuModules.invokeFeature(name, [], () => {})"
    return page.evaluate(f"name => {'(' + source + ')' if wait else 'void ' + source}", name)


def _context(browser, width=1280):
    context = browser.new_context(service_workers='block', viewport={'width': width, 'height': 900})
    context.add_init_script("""
      localStorage.setItem('tofu_onboarding_v1_done', '1');
      window.addEventListener('tofu:app-ready', () => { window.__testAppReady = true; });
      // Execute declared idle work deterministically, including nested slices.
      window.requestIdleCallback = callback => setTimeout(() => callback({
        didTimeout: false, timeRemaining: () => 50,
      }), 0);
      window.cancelIdleCallback = clearTimeout;
    """)
    return context


@pytest.mark.parametrize('width', [390, 1280])
def test_optional_styles_follow_feature_intent_and_settings_waits_before_open(browser, live_server, width):
    from playwright.sync_api import expect
    settings = _styles('frontend/src/features/settings.ts')
    evolution = _styles('frontend/src/features/evolution.ts')
    research = _styles('frontend/src/features/evolution/research/page.ts')
    context = _context(browser, width)
    page = context.new_page()
    requests, held, errors = [], [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    for asset in settings:
        page.route('**/' + asset, lambda route: held.append(route))
    seen = lambda assets: [url for url in requests if any(url.endswith('/' + asset) for asset in assets)]
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _ready(page)
        # Settle past the legitimate delayed onboarding decision.
        page.wait_for_timeout(750)
        assert not seen(settings | evolution | research), requests
        assert not any('/settings.css' in url for url in requests)
        _invoke(page, 'openSettings', wait=False)
        page.wait_for_function("document.querySelector('link[href*=" + json.dumps(next(iter(settings))) + "]') !== null")
        assert held, 'The browser must request CSS before invoking the feature'
        expect(page.locator('#settingsModal')).not_to_have_class('modal-overlay open')
        for route in held:
            route.continue_()
        expect(page.locator('#settingsModal.open')).to_be_visible(timeout=20000)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.evaluate("""assets => assets.every(asset => [...document.styleSheets].some(sheet =>
          sheet.href?.endsWith('/' + asset) && sheet.cssRules.length > 0))""", list(settings))
        _invoke(page, 'closeSettings')
        _invoke(page, 'openSettings')
        assert len(seen(settings)) == len(settings), seen(settings)
        _invoke(page, 'closeSettings')
        _invoke(page, 'openEvolution')
        expect(page.locator('#evolutionPageContainer .evo-main')).to_be_visible(timeout=20000)
        assert seen(evolution)
        assert not seen(research)
        page.evaluate("location.hash = '/evolution/research'")
        expect(page.locator('.evo-research-shell')).to_be_visible(timeout=20000)
        assert seen(research)
        assert not errors, errors
    finally:
        for route in held:
            try:
                route.abort()
            except PlaywrightError:
                pass  # Resolved routes no longer own a request.
        context.close()


def test_failed_settings_stylesheet_uses_bounded_feature_reload_recovery(browser, live_server):
    from playwright.sync_api import expect
    assets = _styles('frontend/src/features/settings.ts')
    failed_asset = sorted(assets)[0]
    context = _context(browser)
    page = context.new_page()
    attempts, documents, errors = [], [], []

    def fail_once(route):
        attempts.append(route.request.url)
        if len(attempts) == 1:
            route.fulfill(status=503, content_type='text/css', body='Unavailable stylesheet')
        else:
            route.continue_()

    page.route('**/' + failed_asset, fail_once)
    page.on('request', lambda request: documents.append(request.url) if request.is_navigation_request() else None)
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _ready(page)
        _invoke(page, 'openSettings', wait=False)
        expect(page.locator('#settingsModal.open')).to_be_visible(timeout=30000)
        assert len(attempts) == 2, attempts
        assert len(documents) == 2, documents
        assert not errors, errors
    finally:
        context.close()


def test_permanent_stylesheet_failure_keeps_feature_closed_after_one_reload(browser, live_server):
    from playwright.sync_api import expect
    failed_asset = sorted(_styles('frontend/src/features/settings.ts'))[0]
    context = _context(browser)
    page = context.new_page()
    attempts, documents = [], []

    def fail(route):
        attempts.append(route.request.url)
        route.fulfill(status=503, content_type='text/css', body='Unavailable stylesheet')

    page.route('**/' + failed_asset, fail)
    page.on('request', lambda request: documents.append(request.url) if request.is_navigation_request() else None)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        _ready(page)
        _invoke(page, 'openSettings', wait=False)
        page.wait_for_function("sessionStorage.getItem('tofu:feature-load-reload') !== null")
        page.wait_for_function("window.__testAppReady && sessionStorage.getItem('tofu:feature-load-pending') === null")
        page.wait_for_timeout(200)
        result = page.evaluate("""async () => {
          try { await window.TofuModules.invokeFeature('openSettings', [], () => {}); return 'Unexpected success'; }
          catch (error) { return error.message; }
        }""")
        assert result.startswith('Unable to preload CSS for '), result
        expect(page.locator('#settingsModal')).not_to_have_class('modal-overlay open')
        assert len(attempts) == len(documents) == 2, (attempts, documents)
    finally:
        context.close()


def test_installed_feature_port_owns_css_failure_before_app_ready(browser, live_server):
    from playwright.sync_api import expect
    failed_asset = sorted(_styles('frontend/src/features/settings.ts'))[0]
    context = _context(browser)
    # Hold the first document's readiness notification. This models the window
    # where the typed port is callable while locale/navigation boot settles.
    context.add_init_script("""(() => {
      const dispatch = window.dispatchEvent.bind(window);
      window.dispatchEvent = event => {
        if (event.type === 'tofu:app-ready'
            && !sessionStorage.getItem('tofu:feature-load-reload')) return true;
        return dispatch(event);
      };
    })();""")
    page = context.new_page()
    attempts = []

    def fail_once(route):
        attempts.append(route.request.url)
        if len(attempts) == 1:
            route.fulfill(status=503, content_type='text/css', body='Unavailable stylesheet')
        else:
            route.continue_()

    page.route('**/' + failed_asset, fail_once)
    try:
        page.goto(live_server, wait_until='domcontentloaded')
        page.wait_for_function('window.TofuModules?.version === 3')
        assert not page.evaluate('Boolean(window.__testAppReady)')
        _invoke(page, 'openSettings', wait=False)
        expect(page.locator('#settingsModal.open')).to_be_visible(timeout=30000)
        assert len(attempts) == 2
        assert page.evaluate("sessionStorage.getItem('tofu:feature-load-reload') !== null")
        assert page.evaluate("sessionStorage.getItem('tofu:vite-preload-reload') === null")
    finally:
        context.close()

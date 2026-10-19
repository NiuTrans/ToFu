"""Real-browser contract for backend-specific database path controls.

The application server/assets are real; only storage status and path-save
responses are intercepted, so this never reconfigures a user's authority.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.visual


def _status(backend: str) -> dict:
    return {
        'engine': {'backend': backend, 'deploymentMode': 'personal', 'ready': True,
                   'state': 'ready', 'dataRoot': '/network/data',
                   'authorityPath': '/network/data/tofudb', 'dataBytes': 1000,
                   'filesystem': {'class': 'network-filesystem', 'freeBytes': 10**12},
                   'engines': {'sqlite': {'available': True}, 'tofudb': {'available': True}}},
        'fastpath': {'supported': True, 'enabled': backend == 'sqlite',
                     'kind': 'native' if backend == 'tofudb' else 'sqlite',
                     'mode': 'auto' if backend == 'sqlite' else 'off',
                     'directory': '/persistent/local' if backend == 'sqlite' else '',
                     'active': backend == 'sqlite',
                     'activeDirectory': '/persistent/local' if backend == 'sqlite' else '',
                     'restartRequired': False, 'locked': False, 'lockReason': '',
                     'authorityPath': '/network/data', 'shadowPath': ''},
        'searchProjection': ({
            'required': True, 'directory': '/network/data/projections',
            'configuredDirectory': '', 'defaultDirectory': '/network/data/projections',
            'pendingDirectory': '/network/data/projections', 'restartRequired': False,
        } if backend == 'tofudb' else None),
        'backends': {'current': backend},
        'preferences': {'backup': {'enabled': False, 'directory': ''}},
        'migration': {'job': None},
    }


def test_database_projection_paths_and_sqlite_toggle_in_real_browser(page, live_server, screenshot_dir):
    status = _status('tofudb')
    requests = []
    page.route('**/api/v4/database/status*', lambda route: route.fulfill(json=status))
    page.route('**/api/v4/database/paths?*', lambda route: route.fulfill(json={'candidates': []}))
    page.route('**/api/v4/database/paths', lambda route: route.fulfill(json={
        'candidates': [{'path': '/tmp', 'storageClass': 'local-block',
                        'persistence': 'ephemeral', 'volatile': True,
                        'isCurrent': False, 'recommended': True,
                        'freeBytes': 10 * 1024**3, 'writable': True, 'latencyMs': 0.5}]}))

    def apply(route):
        payload = route.request.post_data_json
        requests.append(payload)
        if 'searchProjection' in payload:
            configured = payload['searchProjection']['directory']
            state = status['searchProjection']
            state.update(configuredDirectory=configured,
                         pendingDirectory=(configured + '/namespaced-index' if configured else state['defaultDirectory']))
            state['restartRequired'] = state['pendingDirectory'] != state['directory']
            route.fulfill(json={'ok': True, 'restartRequired': state['restartRequired'],
                                'searchProjection': state})
        else:
            status['fastpath']['enabled'] = payload['fastPath']['enabled']
            route.fulfill(json={'ok': True, 'restartRequired': True})

    page.route('**/api/v4/database/paths', lambda route: apply(route) if route.request.method == 'POST' else route.fallback())
    page.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1'); localStorage.setItem('tofu_ui_lang', 'en')")
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("typeof window.openSettings === 'function' && typeof window.switchSettingsTab === 'function'")
    page.evaluate("""() => {
      window.openSettings(); window.switchSettingsTab('database');
    }""")
    page.wait_for_selector('#dbPathCurrent', state='visible')
    assert page.locator('#settingsTab_database details[open]').count() == 0
    assert not page.locator('#dbSearchProjectionCustom').is_visible()
    assert not requests
    page.screenshot(path=str(Path(screenshot_dir) / 'database-summary-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    assert not page.locator('#settingsTab_database').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    page.screenshot(path=str(Path(screenshot_dir) / 'database-summary-mobile.png'))
    page.set_viewport_size({'width': 1280, 'height': 800})
    page.locator('#dbFastPathDisclosure > summary').click()
    page.locator('#dbSearchProjectionDisclosure > summary').click()
    page.wait_for_selector('#dbSearchProjectionCustom', state='visible')
    assert page.locator('#dbFastPathToggle').is_visible()
    assert not page.locator('#dbFastPathToggle').is_checked()
    assert page.locator('#dbFastPathToggle').is_enabled()
    # An off Fast Path has no candidate rows; the editable directory remains
    # available without rendering a decorative empty picker box.
    assert page.locator('#dbFastPathCustom').is_visible()
    assert page.locator('#dbSearchProjectionCustom').is_enabled()
    page.locator('#dbSearchProjectionCustom').fill('/tmp/native-index')
    page.locator('#dbSearchProjectionApply').click()
    page.wait_for_function("document.querySelector('#dbSearchProjectionStatus').textContent.includes('/tmp/native-index')")
    assert requests[-1] == {'searchProjection': {'directory': '/tmp/native-index'}}
    state_text = page.locator('#dbSearchProjectionStatus').inner_text()
    assert '/network/data/projections' in state_text
    assert '/tmp/native-index/namespaced-index' in state_text
    page.locator('.app-dialog-cancel').click()
    page.wait_for_function("!document.querySelector('#dbSearchProjectionReset').disabled")
    before_reset = len(requests)
    page.locator('#dbSearchProjectionReset').click()
    assert len(requests) == before_reset
    page.locator('#dbSearchProjectionApply').click()
    page.wait_for_function("document.querySelector('#dbSearchProjectionReset').disabled")
    assert requests[-1] == {'searchProjection': {'directory': ''}}
    page.wait_for_function("!document.querySelector('#dbBackupToggle').disabled")
    page.locator('#dbBackupDisclosure > summary').click()
    page.locator('label:has(#dbBackupToggle)').click()
    assert page.locator('#dbBackupToggle').is_checked()
    page.wait_for_selector('#dbBackupField', state='visible')
    assert page.locator('#dbBackupToggle').is_checked()
    assert page.locator('#dbBackupCandidates .db-candidate').count() == 0

    # CSS gates cannot detect unresolved custom properties or flex shrink that
    # wraps Chinese action labels. Inspect real computed layout as well.
    badge = page.locator('#dbSearchProjectionCandidates .db-candidate-badge').first
    assert badge.evaluate('(el) => getComputedStyle(el).backgroundColor') != 'rgba(0, 0, 0, 0)'
    for button_id in ('dbSearchProjectionApply', 'dbBackupApply'):
        assert page.locator('#' + button_id).evaluate('''(el) => {
            const range = document.createRange(); range.selectNodeContents(el);
            return range.getClientRects().length === 1;
        }'''), 'Path action label must stay on one line'

    page.screenshot(path=str(Path(screenshot_dir) / 'database-projection-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    overflow = page.locator('#settingsTab_database').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    assert not overflow, 'Database path controls overflow a narrow settings panel'

    assert page.locator('#dbSearchProjectionApply').evaluate('''(el) => {
        const range = document.createRange(); range.selectNodeContents(el);
        return range.getClientRects().length === 1;
    }''')
    page.screenshot(path=str(Path(screenshot_dir) / 'database-projection-mobile.png'))
    page.set_viewport_size({'width': 1280, 'height': 800})

    status.clear()
    status.update(_status('sqlite'))
    page.evaluate('window._populateDatabaseTab()')
    page.wait_for_selector('#dbFastPathToggle', state='attached')
    page.wait_for_function("!document.querySelector('#dbFastPathRow').hidden && document.querySelector('#dbFastPathToggle').checked")
    switch = page.locator('#dbFastPathToggle')
    assert switch.is_enabled() and switch.is_checked()
    before_disable = len(requests)
    page.locator('label:has(#dbFastPathToggle)').click()
    assert len(requests) == before_disable
    page.locator('#dbFastPathCancel').click()
    assert switch.is_checked()
    page.locator('label:has(#dbFastPathToggle)').click()
    page.locator('#dbFastPathApply').click()
    page.locator('.app-dialog-cancel').click()
    page.wait_for_function("document.querySelector('#dbFastPathToggle').checked === false && !document.querySelector('#dbFastPathToggle').disabled")
    assert requests[-1] == {'fastPath': {'enabled': False, 'directory': ''}}
    assert not switch.is_checked()


def _open_database(page, live_server):
    page.add_init_script("localStorage.setItem('tofu_onboarding_v1_done', '1'); localStorage.setItem('tofu_ui_lang', 'en')")
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("typeof window.openSettings === 'function' && typeof window.switchSettingsTab === 'function'")
    page.evaluate("() => { window.openSettings(); window.switchSettingsTab('database'); }")


def test_database_initial_loading_and_failed_status_offer_honest_retry(page, live_server, screenshot_dir):
    held = []
    state = {'phase': 'hold'}

    def status_response(route):
        if state['phase'] == 'ready':
            route.fulfill(json=_status('tofudb'))
        elif state['phase'] == 'failed':
            route.fulfill(status=503, json={'ok': False, 'error': 'status unavailable'})
        else:
            held.append(route)

    page.route('**/api/v4/database/status*', status_response)
    page.route('**/api/v4/database/paths*', lambda route: route.fulfill(json={'candidates': []}))
    _open_database(page, live_server)
    page.wait_for_function("document.querySelector('#settingsTab_database').getAttribute('aria-busy') === 'true'")
    assert page.locator('#dbStatusMessage').is_visible()
    assert not page.locator('#dbFastPathRow').is_visible()
    assert not page.locator('#dbSearchProjection').is_visible()
    assert page.locator('#dbEngineCard').is_visible()
    assert page.locator('#dbEngineTofudb').is_disabled()
    page.screenshot(path=str(Path(screenshot_dir) / 'database-initial-loading.png'))
    assert held, 'Status request must have been sent'
    state['phase'] = 'failed'
    for route in held:
        route.fulfill(status=503, json={'ok': False, 'error': 'status unavailable'})
    page.wait_for_function("document.querySelector('#settingsTab_database').getAttribute('aria-busy') === 'false'")
    assert page.locator('#dbPathError').is_visible()
    assert page.locator('#dbPathRefresh').is_enabled()
    assert not page.locator('#dbFastPathRow').is_visible()
    state['phase'] = 'ready'
    page.locator('#dbPathRefresh').click()
    page.wait_for_selector('#dbFastPathStatus', state='visible')
    page.locator('#dbFastPathDisclosure > summary').click()
    assert page.locator('#dbFastPathToggle').is_enabled()
    assert not page.locator('#dbFastPathToggle').is_checked()
    assert page.locator('#dbSearchProjection').is_visible()
    assert not page.locator('#dbStatusMessage').is_visible()


def test_native_fastpath_pending_and_locked_state_is_separate_from_index(page, live_server, screenshot_dir):
    status = _status('tofudb')
    status['fastpath'].update(enabled=True, directory='/persistent/native-base',
                             restartRequired=True, active=False)
    page.route('**/api/v4/database/status*', lambda route: route.fulfill(json=status))
    page.route('**/api/v4/database/paths*', lambda route: route.fulfill(json={'candidates': []}))
    _open_database(page, live_server)
    page.wait_for_selector('#dbFastPathStatus', state='visible')
    page.locator('#dbFastPathDisclosure > summary').click()
    page.locator('#dbSearchProjectionDisclosure > summary').click()
    page.wait_for_selector('#dbFastPathField', state='visible')
    assert page.locator('#dbFastPathToggle').is_checked()
    text = page.locator('#dbFastPathDetail').inner_text()
    assert '/persistent/native-base' in text
    assert '重启' in text or 'restart' in text.lower()
    assert page.locator('#dbSearchProjectionCustom').is_enabled()
    assert page.locator('#dbFastPathCustom').is_enabled()
    status['fastpath'].update(active=True, activeDirectory='/persistent/native-base/tofudb-fastpath/namespace',
                             authorityPath='/persistent/native-base/tofudb-fastpath/namespace',
                             shadowPath='/network/data/tofudb-fastpath-shadow/namespace',
                             restartRequired=False, locked=True,
                             lockReason='Stop the application and use offline tofu-db fastpath-export before changing selection.')
    page.locator('#dbPathRefresh').click()
    page.wait_for_function("document.querySelector('#dbFastPathLockSummary').textContent.includes('fastpath-export')")
    assert not page.locator('#dbFastPathField').is_visible()
    assert page.locator('#dbFastPathToggle').is_checked()
    assert page.locator('#dbSearchProjectionCustom').is_enabled()
    text = page.locator('#dbFastPathDetail').inner_text()
    assert 'fastpath-export' in text
    assert '/network/data/tofudb-fastpath-shadow/namespace' in text
    page.screenshot(path=str(Path(screenshot_dir) / 'database-native-fastpath-locked.png'))


def _database_text_contrast(page):
    return page.locator('''#settingsTab_database .settings-section-title,
        #settingsTab_database .db-save-note, #settingsTab_database .db-card-title,
        #settingsTab_database .db-meta-label, #settingsTab_database .db-config-status,
        #settingsTab_database .db-disclosure-action, #dbEngineState''').evaluate_all(r'''elements => {
      const color = value => {
        const values = value.match(/[\d.]+/g).map(Number);
        return [...values.slice(0,3).map(v => value.startsWith('color(srgb') ? v : v / 255), values[3] ?? 1];
      };
      const blend = (fg,bg) => fg.slice(0,3).map((v,i) => v * fg[3] + bg[i] * (1 - fg[3]));
      const background = el => {
        if (!el) return [1,1,1];
        const c = color(getComputedStyle(el).backgroundColor);
        return c[3] === 1 ? c.slice(0,3) : blend(c,background(el.parentElement));
      };
      const luminance = c => c.map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4)
        .reduce((sum,v,i) => sum + v * [.2126,.7152,.0722][i],0);
      return elements.filter(el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden')
        .map(el => {
          const bg = background(el), fg = blend(color(getComputedStyle(el).color),bg);
          const a = luminance(fg), b = luminance(bg);
          return {text:el.textContent, ratio:(Math.max(a,b)+.05)/(Math.min(a,b)+.05)};
        });
    }''')


@pytest.mark.parametrize('theme', ['tofu', 'light', 'dark'])
@pytest.mark.parametrize('language', ['zh', 'en'])
def test_database_layout_keyboard_and_read_only_refresh(
    page, live_server, screenshot_dir, assert_no_js_errors, theme, language,
):
    """Consistent summaries, exact evidence and drafts at every supported width."""
    status = _status('tofudb')
    long_path = '/network/' + 'long-authority-segment/' * 12 + 'tofudb'
    status['engine']['authorityPath'] = long_path
    counts = {'status': 0, 'paths': 0}
    mutations = []

    def database_response(route):
        if route.request.method != 'GET':
            mutations.append(route.request.url)
            route.fulfill(status=403, json={'error': 'Writes forbidden in layout test'})
        elif '/status' in route.request.url:
            counts['status'] += 1
            route.fulfill(json=status)
        elif '/paths' in route.request.url:
            counts['paths'] += 1
            route.fulfill(json={'candidates': []})
        else:
            route.fulfill(status=404, json={'error': 'Unexpected database read'})

    page.route('**/api/v4/database/**', database_response)
    page.add_init_script(f"""localStorage.setItem('tofu_onboarding_v1_done','1');
      localStorage.setItem('tofu_ui_lang', {language!r});
      localStorage.setItem('claude_ui_theme', {theme!r});""")
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.goto(live_server, wait_until='networkidle')
    page.wait_for_function("typeof window.openSettings === 'function' && typeof window.switchSettingsTab === 'function'")
    page.evaluate("() => { window.openSettings(); window.switchSettingsTab('database'); }")
    page.wait_for_function("document.querySelector('#dbEngineName').textContent === 'TofuDB'")

    assert page.locator('html').get_attribute('data-theme') == theme
    assert page.locator('#settingsTab_database details[open]').count() == 0
    assert counts['paths'] == 0 and not mutations
    assert page.locator('#dbPathCurrent .db-overview-item').count() == 2

    for editor in ('dbFastPathCustom', 'dbSearchProjectionCustom', 'dbBackupCustom'):
        assert not page.locator('#' + editor).is_visible()
    collapsed_heights = {}
    contrast = _database_text_contrast(page)
    assert contrast and all(row['ratio'] >= 4.5 for row in contrast), contrast
    page.screenshot(path=str(Path(screenshot_dir) / f'database-{theme}-{language}-summary.png'))

    for width in (1280, 768, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})

        collapsed_heights[width] = page.locator('#dbPathCard').evaluate('(el) => el.getBoundingClientRect().height')
        assert not page.locator('#settingsTab_database').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
        summaries = page.locator('.db-config-summary').evaluate_all('''(rows) => rows.map(el => {
          const r=el.getBoundingClientRect();
          return {height:r.height, children:[...el.children].map(child => {
            const c=child.getBoundingClientRect();
            return c.left >= r.left-1 && c.right <= r.right+1;
          })};
        })''')
        assert all(row['height'] >= 44 and all(row['children']) for row in summaries)
    page.screenshot(path=str(Path(screenshot_dir) / f'database-{theme}-{language}-mobile.png'))

    # Native keyboard disclosure must work even after custom summary styling.
    storage = page.locator('#dbStorageDisclosure > summary')
    storage.focus()
    storage.press('Enter')
    assert page.locator('#dbEngineMeta').is_visible()
    assert long_path in page.locator('#dbEngineMeta').inner_text()
    assert counts['paths'] == 0
    for name in ('dbFastPathDisclosure', 'dbSearchProjectionDisclosure', 'dbBackupDisclosure'):
        summary = page.locator('#' + name + ' > summary')
        summary.focus()
        summary.press('Space')
        page.wait_for_function('(id) => document.getElementById(id).open', arg=name)
    assert counts['paths'] == 1
    draft = '/draft/search-index-base'
    page.locator('#dbSearchProjectionCustom').fill(draft)

    # Collapsing a native disclosure must reclaim space without losing an
    # unapplied edit or performing an implicit save / candidate rescan.
    projection_summary = page.locator('#dbSearchProjectionDisclosure > summary')
    expanded_height = page.locator('#dbPathCard').evaluate('(el) => el.getBoundingClientRect().height')
    projection_summary.focus()
    projection_summary.press('Enter')
    assert not page.locator('#dbSearchProjectionCustom').is_visible()
    assert page.locator('#dbPathCard').evaluate('(el) => el.getBoundingClientRect().height') < expanded_height
    projection_summary.press('Enter')
    assert page.locator('#dbSearchProjectionCustom').is_visible()
    assert page.locator('#dbSearchProjectionCustom').input_value() == draft
    assert counts['paths'] == 1 and not mutations
    before_refresh = counts['status']
    page.locator('#dbPathRefresh').click()
    page.wait_for_function("document.querySelector('#settingsTab_database').getAttribute('aria-busy') === 'false'")
    assert counts['status'] == before_refresh + 1
    assert page.locator('#dbSearchProjectionCustom').input_value() == draft
    assert page.locator('#settingsTab_database details[open]').count() == 4
    assert counts['paths'] == 1 and not mutations
    for width in (1440, 390, 320):
        page.set_viewport_size({'width': width, 'height': 1000})
        assert not page.locator('#settingsTab_database').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')

        if width in collapsed_heights:
            assert page.locator('#dbPathCard').evaluate('(el) => el.getBoundingClientRect().height') > collapsed_heights[width]
        # Also verify individual controls remain inside the viewport after
        # scrolling them into view, not only the panel's scroll dimensions.
        for control in ('dbSearchProjectionCustom', 'dbSearchProjectionApply', 'dbPathRefresh'):
            locator = page.locator('#' + control)
            locator.scroll_into_view_if_needed()
            assert locator.evaluate('''el => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.left >= -1 && rect.right <= innerWidth + 1;
            }'''), f'{control} must remain reachable at {width}px'
        if width == 390:
            page.locator('#dbSearchProjectionCustom').scroll_into_view_if_needed()
            page.screenshot(path=str(Path(screenshot_dir) / f'database-{theme}-{language}-expanded-mobile.png'))
    page.set_viewport_size({'width': 1440, 'height': 1000})
    page.screenshot(path=str(Path(screenshot_dir) / f'database-{theme}-{language}-expanded.png'))

    status['fastpath'].update(active=True, enabled=True, locked=True,
                             restartRequired=True, lockReason='Offline export required: ' + long_path)
    page.locator('#dbPathRefresh').click()
    page.wait_for_function("document.querySelector('#dbFastPathLockSummary').textContent.includes('Offline export required')")
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.locator('#dbFastPathLockSummary').is_visible()
    assert page.locator('#dbFastPathToggle').is_disabled()
    assert page.locator('#dbFastPathStatus').evaluate('(el) => el.scrollHeight <= el.clientHeight + 1')
    assert not page.locator('#settingsTab_database').evaluate('(el) => el.scrollWidth > el.clientWidth + 2')
    assert not mutations

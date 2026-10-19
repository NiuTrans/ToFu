"""Restart card follows actual health phases through a slow boot and reloads.

Rendering gate on a local HTTP server with controlled transport responses;
boot-route and native recovery authority are covered by their owning suites.
"""
import pytest
from tests._runtime_sections import native_module_path, runtime_section_path
from tests.support.frontend_visual import (
    ROOT, frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual


def mount_restart(page):
    page.evaluate('''() => {
      document.body.innerHTML = '<button id="updateRestartNowBtn"><span>Restart</span></button><button id="updateShutdownBtn">Shutdown</button><div id="updateModal"><div id="updateModalBody"></div></div>';
      window.debugLog = () => {};
      window._onReady = () => {};
      window.escapeHtml = value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;');
      window.t = key => key;
      window.showToast = () => {};
      window.showConfirm = async () => true;
      window.BackendAvailabilityRestartScope = {begin(){},end(){}};
      window.restartPosts = 0;
      window.Api = {
        health: {info: async () => (await fetch('/api/health')).json(),
          retryBoot: async () => (await fetch('/api/v4/boot/retry',{method:'POST'})).json()},
        update: {restart: async () => {restartPosts++; return {ok:true,restarting:true};}},
      };
      const actualNow = Date.now;
      window.clockAdvance = 0;
      Date.now = () => actualNow() + clockAdvance;
    }''')
    page.add_script_tag(path=native_module_path(
        '.native/restart-observation-visual.js', ROOT / 'frontend/src/features/settings/restart-observation.ts'))
    page.add_script_tag(path=runtime_section_path('update.js'))


def test_slow_boot_stays_owned_then_reloads(frontend_visual_page):
    page = frontend_visual_page
    payload = {'ok': True, 'bootId': 'old', 'boot': {'status': 'ready'}}
    page.route('**/api/health', lambda route: route.fulfill(json=payload))
    mount_restart(page)
    page.evaluate('restartServer()')
    payload.update(bootId='new', boot={'status':'running','phaseTotal':9,
        'currentPhase':{'index':3,'key':'storage_authority_sql'}})
    page.evaluate('clockAdvance = 270000')
    page.wait_for_function("document.getElementById('updRestartPhase').textContent.includes('storage_authority_sql')")
    page.wait_for_function("document.getElementById('updRestartSub').textContent === 'update.restartTimeout'")
    assert page.locator('#updateRestartNowBtn').is_disabled()
    assert page.locator('#updRestartCard').is_visible()
    assert not page.locator('#updateModal').evaluate("node => node.classList.contains('open')")
    assert page.locator('#restart-progress-dock #updRestartCard').count() == 1
    assert not page.locator('#updRestartCard').evaluate("node => node.classList.contains('is-error')")
    # Direct re-entry and repeated button interaction both retain one attempt.
    page.evaluate('restartServer()')
    assert page.evaluate('restartPosts') == 1
    page.locator('#updRestartCard').screenshot(path='/tmp/tofu-restart-slow-boot.png')
    payload['boot'] = {'status':'ready'}
    page.get_by_role('button', name='boot.reload').wait_for()
    with page.expect_event('framenavigated'):
        page.get_by_role('button', name='boot.reload').click()
    assert page.url.startswith('http://127.0.0.1:')


def test_failed_boot_retries_phase_without_another_restart(frontend_visual_page):
    page = frontend_visual_page
    payload = {'ok':True,'bootId':'existing','boot':{'status':'running','phaseTotal':9,
        'currentPhase':{'index':3,'key':'storage_authority_sql'}}}
    page.route('**/api/health', lambda route: route.fulfill(json=payload))
    retries = []
    def retry(route):
        retries.append(route.request.method)
        payload['boot'] = {'status':'running','phaseTotal':9,
            'currentPhase':{'index':3,'key':'storage_authority_sql'}}
        route.fulfill(json={'ok':True})
    page.route('**/api/v4/boot/retry', retry)
    mount_restart(page)
    page.evaluate('restartServer()')
    assert page.evaluate('restartPosts') == 0
    payload['boot'] = {'status':'failed','failedPhase':{'index':3,'key':'storage_authority_sql'},
        'error':'injected storage failure'}
    page.wait_for_function("document.getElementById('updRestartTitle').textContent === 'boot.failedTitle'")
    assert 'injected storage failure' in page.locator('#updRestartCard').inner_text()
    page.evaluate('restartServer()')
    assert retries == ['POST']
    assert page.evaluate('restartPosts') == 0
    assert page.locator('#updateRestartNowBtn').is_disabled()
    page.evaluate('_restartStopTimers(); cancelAnimationFrame(_restartRaf)')


def test_missing_replacement_waits_without_claiming_readiness(frontend_visual_page):
    page = frontend_visual_page
    payload = {'ok': True, 'bootId': 'old', 'boot': {'status': 'ready'}}
    offline = False
    def health(route):
        if offline:
            route.abort('connectionrefused')
        else:
            route.fulfill(json=payload)
    page.route('**/api/health', health)
    mount_restart(page)
    page.evaluate('restartServer()')
    offline = True
    page.evaluate('clockAdvance = 3254000')
    page.wait_for_function("document.getElementById('updRestartPhase').textContent.includes('update.phase.bind')")
    assert 'update.phase.readiness' not in page.locator('#updRestartCard').inner_text()
    assert page.locator('#updRestartPct').inner_text() == '20%'
    assert page.locator('#updateRestartNowBtn').is_disabled()
    page.locator('#updRestartCard').screenshot(path='/tmp/tofu-restart-no-listener.png')
    assert page.evaluate('restartPosts') == 1
    page.evaluate('_restartStopTimers(); cancelAnimationFrame(_restartRaf)')

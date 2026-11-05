"""Coordinator navigation with real Chromium, transport and stalled HTTP reads.

The local fixture has no application storage or model calls. Read-only routes
inject latency; production surface, endpoint facade and cancellation stay real.
"""
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from tests._runtime_sections import ROOT, native_module_path
from tests.support.frontend_visual import (
    frontend_visual_origin as frontend_visual_origin,
    frontend_visual_page as frontend_visual_page,
)

pytestmark = pytest.mark.visual


def test_coordinator_opens_with_stalled_details_and_recovers_after_scope_timeout(frontend_visual_page, tmp_path):
    page = frontend_visual_page
    entry = tmp_path / 'coordinator-loading.ts'
    entry.write_text(f"""
export {{ createProjectCoordinationSurface }} from '{ROOT}/frontend/src/features/project-coordination-surface';
export {{ createProjectEndpoints }} from '{ROOT}/frontend/src/api/endpoint-registry/project';
export {{ createEndpointPorts }} from '{ROOT}/frontend/src/api/endpoint-registry/ports';
export {{ apiTransport }} from '{ROOT}/frontend/src/api/transport';
export {{ ready }} from '{ROOT}/frontend/src/i18n';
""")
    page.add_script_tag(path=native_module_path('.native/coordinator-loading.js', entry))
    page.evaluate('async () => { await ready(); }')
    page.clock.install()
    requests, failed, held = [], [], []
    recovered = False
    offline, rejected = set(), set()
    page.on('requestfailed', lambda request: failed.append(request.url))

    def respond(route):
        parsed = urlparse(route.request.url)
        path = parse_qs(parsed.query)['path'][0]
        requests.append((parsed.path, path))
        assert route.request.method == 'GET'
        if parsed.path.endswith('/coordination') and path in offline:
            route.abort('internetdisconnected')
            return
        if parsed.path.endswith('/coordination') and path in rejected:
            route.fulfill(status=400, json={'error': 'path is required'})
            return
        if parsed.path.endswith('/coordination') and (path not in ('/beta', '/slow') or recovered):
            route.fulfill(json={'meta': {}, 'data': {
                'projectPath': path, 'conversationId': path[1:] + '-leader', 'revision': 1,
                'availability': 'available',
                'capabilities': {'assign': False, 'sendMessage': False, 'steerSubagent': False},
            }})
        else:
            held.append(route)

    page.route('**/api/v4/project/**', respond)
    page.evaluate('''() => {
      document.body.innerHTML='<aside id="sidebar"></aside><main id="canvas"></main>';
      const api = createProjectEndpoints(createEndpointPorts(apiTransport, () => 'en'));
      window.projectPath = '/alpha'; window.opened = [];
      window.loadingSurface = createProjectCoordinationSurface({
        sidebar: document.querySelector('#sidebar'), canvas: document.querySelector('#canvas'), mode: 'sidebar',
        sidebarPath: () => window.projectPath,
        scope: () => ({ path: window.projectPath, currentPath: () => window.projectPath, currentConversationId: '',
          binding: api.coordination, board: api.board, bind: api.bindCoordinator, openEvidence: () => {},
          reports: { read: api.coordinatorReports, markRead: api.markCoordinatorReportRead },
          progress: { read: api.coordinatorProgress },
          dispatch: { list: api.coordinationAssignments }, navigate: id => window.opened.push(id) }),
        details: () => {}, create: () => {},
      });
      void window.loadingSurface.refresh();
      window.selectProject = path => { window.projectPath = path; void window.loadingSurface.refresh(); };
    }''')
    row = page.locator('#sidebar .coordination-leader')
    expect(row).to_have_attribute('role', 'button')
    expect(row).to_have_attribute('data-coordinator-id', 'alpha-leader')
    row.click()
    assert page.evaluate('window.opened') == ['alpha-leader']
    assert ('/api/v4/project/coordination/progress', '/alpha') in requests
    assert page.locator('#canvas').inner_text() == ''

    with page.expect_request('**/project/coordination?path=%2Fbeta'):
        page.evaluate("selectProject('/beta')")
    expect(row).to_have_attribute('aria-busy', 'true')
    page.evaluate("selectProject('/gamma')")
    expect(row).to_have_attribute('data-coordinator-id', 'gamma-leader')
    expect(row).to_have_attribute('role', 'button')
    row.focus(); page.keyboard.press('Enter')
    assert page.evaluate('window.opened') == ['alpha-leader', 'gamma-leader']
    assert any('path=%2Falpha' in url for url in failed)
    assert any('coordination?path=%2Fbeta' in url for url in failed)

    with page.expect_request('**/project/coordination?path=%2Fslow'):
        page.evaluate("selectProject('/slow')")
    page.clock.fast_forward(12_001)
    retry = page.locator('#sidebar .coordination-read-error button')
    expect(retry).to_be_visible()
    expect(row).not_to_have_attribute('aria-busy', 'true')
    recovered = True
    retry.click()
    expect(row).to_have_attribute('role', 'button')
    expect(row).to_have_attribute('data-coordinator-id', 'slow-leader')
    row.click()
    assert page.evaluate('window.opened')[-1] == 'slow-leader'
    # A failed read must recover on lifecycle signals without a project change
    # or a human Retry click. Optional HTTP reads stay stalled throughout.
    for event in ('online', 'visibilitychange'):
        path = '/recover-' + event
        offline.add(path)
        page.evaluate('path => selectProject(path)', path)
        expect(retry).to_be_visible()
        expect(row).not_to_have_attribute('role', 'button')
        offline.remove(path)
        page.evaluate("event => (event === 'online' ? window : document).dispatchEvent(new Event(event))", event)
        expect(row).to_have_attribute('data-coordinator-id', path[1:] + '-leader')
        expect(row).to_have_attribute('role', 'button')
        expect(retry).to_have_count(0)
        row.click()
        assert page.evaluate('window.opened')[-1] == path[1:] + '-leader'

    # A transport failure on an already usable row is no evidence against the
    # binding it confirmed: the row stays usable and carries no warning.
    page.clock.fast_forward(12_001)
    path = '/recover-visibilitychange'
    offline.add(path)
    with page.expect_console_message(lambda message: 'Binding read failed: code=network' in message.text):
        page.evaluate('void loadingSurface.refresh(true)')
    expect(retry).to_have_count(0)
    expect(row).to_have_attribute('role', 'button')
    expect(row).to_have_attribute('data-coordinator-id', path[1:] + '-leader')
    offline.remove(path)
    # A definitive rejection still warns, and the warning clears at binding
    # success, before the progress/report deadline.
    rejected.add(path)
    page.evaluate('void loadingSurface.refresh(true)')
    expect(retry).to_be_visible()
    rejected.remove(path)
    retry.click()
    expect(retry).to_have_count(0)
    expect(row).to_have_attribute('role', 'button')
    page.evaluate('loadingSurface.destroy()')
    expect(page.locator('#sidebar')).to_be_empty()
    assert held

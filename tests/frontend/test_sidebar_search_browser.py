"""Real built sidebar journey with controlled search transport, no model calls.

Controller tests own races; this proves the shipped graph, DOM actions and
localized retry/load-more affordances connect to that controller.
"""
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.visual


def test_search_unloaded_results_retry_and_pagination(page):
    page.wait_for_selector('#userInput', state='visible')
    page.wait_for_function('window.TofuModules && window.TofuModules.version === 3')
    calls, snapshots = [], []
    fail = True

    def hit(cid):
        return {'id': cid, 'matchField': 'originalContent', 'matchRole': 'user',
                'matchSnippet': 'original question needle',
                'conversation': {'id': cid, 'title': 'Remote history ' + cid,
                                 'createdAt': 1, 'updatedAt': 2, 'msgCount': 2,
                                 'rev': 1, 'settings': {}}}

    def serve(route):
        nonlocal fail
        args = parse_qs(urlparse(route.request.url).query)
        calls.append(args)
        if fail:
            fail = False
            route.fulfill(status=503, json={'ok': False, 'error': 'Unavailable'})
            return
        more = 'cursor' not in args
        route.fulfill(json={'ok': True, 'items': [hit('remote-first' if more else 'remote-second')],
                            'nextCursor': 'page-two' if more else None,
                            'coverage': 'ready', 'coverageReasons': []})

    page.route('**/api/v4/conversations/search?*', serve)
    page.on('request', lambda request: snapshots.append(request.url)
            if '/api/v4/conversations/remote-' in request.url else None)
    # Keyless-server onboarding can arrive after the fixture's initial shell check.
    from tests.conftest import _dismiss_onboarding_modals
    _dismiss_onboarding_modals(page)
    page.locator('#sidebarSearchToggle').click()
    page.locator('#sidebarSearchInput').fill('needle')
    retry = page.locator('[data-search-control="sidebar.searchRetry"]')
    retry.wait_for(state='visible')
    assert len(calls) == 1
    retry.click()
    page.locator('.conv-item[data-conv-id="remote-first"]').wait_for(state='visible')
    assert len(calls) == 2
    assert not snapshots  # Server hits do not trigger per-hit transcript hydration.
    more = page.locator('[data-search-control="sidebar.searchLoadMore"]')
    more.click()
    page.locator('.conv-item[data-conv-id="remote-second"]').wait_for(state='visible')
    assert len(calls) == 3 and calls[-1]['cursor'] == ['page-two']
    assert page.locator('.conv-item[data-conv-id="remote-first"]').count() == 1
    assert more.count() == 0
    assert not snapshots
    assert 'sidebar.' not in page.locator('#sidebarSearchStats').inner_text()
    page.locator('#sidebarSearchClear').click()
    page.wait_for_selector('#sidebarSearchWrapper', state='hidden')
    expected = 'console.error: Failed to load resource: the server responded with a status of 503 (Service Unavailable)'
    errors = list(getattr(page, '_tofu_js_errors', []))
    assert errors.count(expected) <= 1  # Exactly one transport failure is intentional.
    assert not [error for error in errors if error != expected], errors


def test_open_unloaded_empty_worker_uses_owned_metadata_not_search_shell(page):
    import uuid
    from tests._seed import seed_conversation
    from tests.conftest import _dismiss_onboarding_modals

    worker = 'worker-' + uuid.uuid4().hex
    project = '/test/empty-worker-' + uuid.uuid4().hex
    seed_conversation(worker, title='Empty diagnostic worker', settings={
        'projectPath': project, '_coordinatorSource': 'coordinator-original'})
    metadata_reads, execution_writes = [], []
    page.on('request', lambda request: metadata_reads.append(request.url)
        if urlparse(request.url).path == '/api/v4/conversations/' + worker else None)
    page.on('request', lambda request: execution_writes.append(request.url)
        if request.method == 'POST' and ('/chat' in request.url or '/dispatch' in request.url) else None)
    page.route('**/api/v4/conversations/search?*', lambda route: route.fulfill(json={
        'ok': True, 'items': [{'id': worker, 'matchField': 'title',
            'conversation': {'id': worker, 'title': 'Empty diagnostic worker',
                'msgCount': 0, 'rev': 0, 'createdAt': 1, 'updatedAt': 2, 'settings': {}}}],
        'nextCursor': None, 'coverage': 'ready', 'coverageReasons': []}))
    _dismiss_onboarding_modals(page)
    page.locator('#sidebarSearchToggle').click()
    page.locator('#sidebarSearchInput').fill(worker)
    row = page.locator(f'.conv-item[data-conv-id="{worker}"]')
    row.wait_for(state='visible')
    assert not metadata_reads  # Search remains passive and metadata-only.
    row.locator('.conv-title').click(position={'x': 4, 'y': 6})
    try:
        page.wait_for_function('(id) => window.activeConvId === id', arg=worker, timeout=10000)
    except Exception as exc:
        state = page.evaluate('() => ({active: window.activeConvId, shells: window.conversations?.map(c => ({id: c.id, turns: c._serverTurnCount})), notices: document.querySelector("#toastContainer")?.textContent})')
        raise AssertionError({'state': state, 'metadataReads': metadata_reads, 'errors': list(getattr(page, '_tofu_js_errors', []))}) from exc
    assert metadata_reads
    snapshot = page.evaluate('() => { const c = window.getActiveConv(); return {id:c.id, project:c.projectPath, source:c._coordinatorSource, turns:c._serverTurnCount}; }')
    assert snapshot == {'id': worker, 'project': project, 'source': 'coordinator-original', 'turns': 0}
    assert not execution_writes
    assert not list(getattr(page, '_tofu_js_errors', []))
